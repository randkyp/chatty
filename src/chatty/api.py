"""
API client: streaming chat completions, model metadata fetching.

Streaming uses a connection-reuse-friendly httpx client with a bounded connect
timeout but an unbounded read timeout (slow generations must not be cut off).
Handles connection errors gracefully (returns error events instead of raising).
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal

import httpx

from chatty.timing import RequestTimings

# Connect quickly-failing but allow unbounded streaming reads.
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)

OPENCODE_SESSION_HEADER = "x-opencode-session"


def _client_version() -> str:
    try:
        return version("chatty")
    except PackageNotFoundError:
        return "dev"


# Identifies the client to providers that reject generic SDK/HTTP-library user agents.
USER_AGENT = f"chatty/{_client_version()}"

# One client per endpoint so TLS handshakes/connections are reused. Credentials
# are request headers, so they do not belong in the cache key (and therefore do
# not linger there after an ephemeral key is cleared or replaced).
_CLIENT_CACHE: dict[str, httpx.Client] = {}


def get_client(base_url: str, api_key: str | None) -> httpx.Client:
    """Return a cached httpx.Client for this endpoint, creating one if needed."""
    client = _CLIENT_CACHE.get(base_url)
    if client is None or client.is_closed:
        client = httpx.Client(timeout=STREAM_TIMEOUT, http2=False)
        _CLIENT_CACHE[base_url] = client
    return client


def close_clients() -> None:
    """Close all cached clients (call on shutdown)."""
    for client in _CLIENT_CACHE.values():
        try:
            client.close()
        except Exception:
            pass
    _CLIENT_CACHE.clear()


def fetch_model_metadata(
    base_url: str, api_key: str | None, model_id: str | None = None, opencode_session: str | None = None
) -> dict[str, Any]:
    """Try to fetch context length and max tokens from /v1/models.

    Match the selected model exactly, or discover the first model if unset.
    Unknown models never borrow another model's limits. On failure, return {}.
    """
    headers = _auth_headers(api_key, opencode_session)
    result: dict[str, Any] = {}
    try:
        resp = httpx.get(f"{base_url}/v1/models", headers=headers, timeout=10.0)
        if resp.status_code != 200:
            return result
        data = resp.json()
        models = data.get("data", []) if isinstance(data, dict) else []
        if not isinstance(models, list):
            return result
        meta = next(
            (m for m in models if isinstance(m, dict) and (model_id is None or m.get("id") == model_id)),
            None,
        )
        if meta is None:
            return result
        # Different backends expose this differently.
        for key in ("context_length", "max_model_len", "context_window"):
            if key in meta:
                value = int(meta[key])
                if value > 0:
                    result["ctx_size"] = value
                break
        for key in ("max_tokens", "max_completion_tokens", "max_generation_len"):
            if key in meta:
                value = int(meta[key])
                if value >= 0:
                    result["genmax"] = value
                break
        # Grab the model id if available.
        if isinstance(meta.get("id"), str) and meta["id"].strip():
            result["model_id"] = meta["id"]
    except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError, TypeError, OverflowError):
        return {}
    return result


def normalize_model_ids(raw_ids: object) -> list[str]:
    """Retain non-empty string IDs, drop exact duplicates, sort case-insensitively.

    Sorting preserves original spelling; ordering is deterministic with the
    original string as a tie-breaker for case-insensitively equal IDs.
    """
    if not isinstance(raw_ids, list):
        return []
    seen: set[str] = set()
    unique: list[str] = []
    for item in raw_ids:
        if not isinstance(item, str):
            continue
        if not item or not item.strip():
            continue
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    unique.sort(key=lambda s: (s.lower(), s))
    return unique


def list_models(base_url: str, api_key: str | None, opencode_session: str | None = None) -> list[str]:
    """Fetch available models from /v1/models, normalized centrally."""
    headers = _auth_headers(api_key, opencode_session)
    try:
        resp = httpx.get(f"{base_url}/v1/models", headers=headers, timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            models = data.get("data", [])
            if not isinstance(models, list):
                return []
            raw_ids = [m.get("id") for m in models if isinstance(m, dict)]
            return normalize_model_ids(raw_ids)
    except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError, AttributeError):
        pass
    return []


def _stream_payload(
    model: str, messages: list[dict[str, Any]], samplers: dict[str, Any], genmax: int, debug: bool
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if genmax > 0:
        payload["max_tokens"] = genmax
    payload.update(samplers)
    if debug:
        print("\n── DEBUG: request payload ──")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        print("── end payload ──\n")
    return payload


StreamEvent = tuple[Literal["delta", "reasoning", "error", "usage", "timings"], str]

# Message keys a provider may expect back for reasoning continuity. Anything
# else attached to a stored turn stays out of the request.
REASONING_FIELDS = ("reasoning_content", "reasoning", "reasoning_details")


@dataclass
class ReasoningAccumulator:
    """Collect reasoning from streamed deltas so it can be replayed verbatim.

    Providers disagree on both the field name and the shape: most send a plain
    ``reasoning_content`` string, OpenRouter adds structured ``reasoning_details``
    entries (encrypted or signed) that arrive in fragments keyed by ``index``.
    Everything is kept exactly as received and never interpreted, because
    signatures and encrypted blobs are only valid unmodified.
    """

    text: str = ""
    text_key: str = "reasoning_content"
    details: list[dict[str, Any]] = field(default_factory=list)
    _details_by_index: dict[int, dict[str, Any]] = field(default_factory=dict, repr=False)

    def feed(self, delta: dict[str, Any]) -> None:
        """Absorb the reasoning fields of one streamed delta."""
        for key in ("reasoning_content", "reasoning"):
            value = delta.get(key)
            if isinstance(value, str) and value:
                if not self.text:
                    self.text_key = key
                self.text += value
                break
        details = delta.get("reasoning_details")
        if isinstance(details, list):
            for detail in details:
                if isinstance(detail, dict):
                    self._merge_detail(detail)

    def _merge_detail(self, detail: dict[str, Any]) -> None:
        index = detail.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            index = None
        existing = self._details_by_index.get(index) if index is not None else None
        if existing is None:
            entry = dict(detail)
            if index is not None:
                self._details_by_index[index] = entry
            self.details.append(entry)
            return
        for key, value in detail.items():
            if key == "index":
                continue
            if key in ("text", "summary", "data") and isinstance(value, str):
                # These arrive in pieces; concatenate this turn's fragment.
                existing[key] = str(existing.get(key, "")) + value
            elif value is not None:
                existing[key] = value

    def as_fields(self) -> dict[str, Any]:
        """Return the assistant message fields to replay, or {} if none arrived."""
        fields: dict[str, Any] = {}
        if self.text:
            fields[self.text_key] = self.text
        if self.details:
            fields["reasoning_details"] = self.details
        return fields


def _stream_events(data: str, reasoning: ReasoningAccumulator | None = None) -> Generator[StreamEvent, None, None]:
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        return
    err = chunk.get("error")
    if err:
        msg = err.get("message") if isinstance(err, dict) else str(err)
        yield "error", f"Server error: {msg}"
        return
    if chunk.get("timings"):
        # Non-standard llama.cpp block: measured prompt/decode durations.
        yield "timings", json.dumps(chunk["timings"])
    if chunk.get("usage"):
        yield "usage", json.dumps(chunk["usage"])
    choices = chunk.get("choices", [])
    if choices:
        delta = choices[0].get("delta", {})
        if reasoning is not None:
            reasoning.feed(delta)
        reasoning_text = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning_text, str) and reasoning_text:
            yield "reasoning", reasoning_text
        content = delta.get("content", "")
        if content:
            yield "delta", content


def stream_chat(
    base_url: str,
    api_key: str | None,
    model: str,
    messages: list[dict[str, Any]],
    samplers: dict[str, Any],
    genmax: int,
    debug: bool = False,
    opencode_session: str | None = None,
    timings: RequestTimings | None = None,
    reasoning: ReasoningAccumulator | None = None,
) -> Generator[StreamEvent, None, None]:
    """Stream a chat completion. Yields structured events.

    On connection/API errors, yields a single event ("error", message) so the
    caller can display it. Otherwise yields ("delta", text) events, ("reasoning",
    text) for providers that stream thinking, and, if the server reports them, a
    final ("usage", json) event carrying token counts.

    *timings*, when given, is stamped as the request progresses so the caller
    can report a per-stage breakdown afterwards. *reasoning*, when given, is
    filled with the raw reasoning fields for replay on later turns.
    """
    payload = _stream_payload(model, messages, samplers, genmax, debug)

    headers = _auth_headers(api_key, opencode_session)
    if debug:
        _print_debug_headers(headers)
    client = get_client(base_url, api_key)

    if timings is not None:
        timings.mark_dispatched()
    try:
        with client.stream(
            "POST",
            f"{base_url}/v1/chat/completions",
            json=payload,
            headers=headers,
        ) as response:
            if timings is not None:
                timings.mark_headers(response.headers)
                timings.error = None if response.status_code == 200 else f"HTTP {response.status_code}"
            if response.status_code != 200:
                # Try to read an error body.
                body = response.read().decode(errors="replace")
                yield "error", f"HTTP {response.status_code}: {body}"
                return

            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    return
                for event in _stream_events(data, reasoning):
                    if timings is not None:
                        timings.observe(*event)
                    yield event
                    if event[0] == "error":
                        return
    except httpx.ConnectError as e:
        yield "error", f"Connection failed: {e}"
    except httpx.HTTPError as e:
        yield "error", f"HTTP error: {e}"
    except Exception as e:
        yield "error", f"Unexpected error: {e}"


async def stream_chat_async(
    base_url: str,
    api_key: str | None,
    model: str,
    messages: list[dict[str, Any]],
    samplers: dict[str, Any],
    genmax: int,
    debug: bool = False,
    opencode_session: str | None = None,
    timings: RequestTimings | None = None,
    reasoning: ReasoningAccumulator | None = None,
) -> AsyncGenerator[StreamEvent, None]:
    """Stream web completions with cancellable reads and task-owned connections."""
    payload = _stream_payload(model, messages, samplers, genmax, debug)
    headers = _auth_headers(api_key, opencode_session)
    if debug:
        _print_debug_headers(headers)

    if timings is not None:
        timings.mark_dispatched()
    try:
        async with httpx.AsyncClient(timeout=STREAM_TIMEOUT, http2=False) as client:
            async with client.stream(
                "POST", f"{base_url}/v1/chat/completions", json=payload, headers=headers
            ) as response:
                if timings is not None:
                    timings.mark_headers(response.headers)
                    timings.error = None if response.status_code == 200 else f"HTTP {response.status_code}"
                if response.status_code != 200:
                    body = (await response.aread()).decode(errors="replace")
                    yield "error", f"HTTP {response.status_code}: {body}"
                    return
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        return
                    for event in _stream_events(data, reasoning):
                        if timings is not None:
                            timings.observe(*event)
                        yield event
                        if event[0] == "error":
                            return
    except httpx.ConnectError as e:
        yield "error", f"Connection failed: {e}"
    except httpx.HTTPError as e:
        yield "error", f"HTTP error: {e}"


def _auth_headers(api_key: str | None, opencode_session: str | None = None) -> dict[str, str]:
    """Build request headers; *opencode_session* enables the OpenCode identity.

    ``None`` means the profile did not opt in, so no User-Agent or session
    header is sent and the client keeps httpx's default behavior.
    """
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if opencode_session is not None:
        headers["User-Agent"] = USER_AGENT
        headers[OPENCODE_SESSION_HEADER] = opencode_session
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _print_debug_headers(headers: dict[str, str]) -> None:
    """Print request headers under --debug, keeping the bearer token out of logs."""
    redacted = {k: ("Bearer ***" if k.lower() == "authorization" else v) for k, v in headers.items()}
    print("\n── DEBUG: request headers ──")
    print(json.dumps(redacted, indent=2, ensure_ascii=False))
    print("── end headers ──\n")
