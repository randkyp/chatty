"""
API client: streaming chat completions, model metadata fetching.

Streaming uses a connection-reuse-friendly httpx client with a bounded connect
timeout but an unbounded read timeout (slow generations must not be cut off).
Handles connection errors gracefully (returns error events instead of raising).
"""

from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any, Literal

import httpx

# Connect quickly-failing but allow unbounded streaming reads.
STREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)

# One client per (base_url, api_key) so TLS handshakes/connections are reused
# across requests within a session. HTTP/2 is enabled where the server supports it.
_CLIENT_CACHE: dict[tuple[str, str | None], httpx.Client] = {}


def get_client(base_url: str, api_key: str | None) -> httpx.Client:
    """Return a cached httpx.Client for this endpoint, creating one if needed."""
    key = (base_url, api_key)
    client = _CLIENT_CACHE.get(key)
    if client is None or client.is_closed:
        client = httpx.Client(timeout=STREAM_TIMEOUT, http2=False)
        _CLIENT_CACHE[key] = client
    return client


def close_clients() -> None:
    """Close all cached clients (call on shutdown)."""
    for client in _CLIENT_CACHE.values():
        try:
            client.close()
        except Exception:
            pass
    _CLIENT_CACHE.clear()


def fetch_model_metadata(base_url: str, api_key: str | None) -> dict[str, Any]:
    """Try to fetch context length and max tokens from /v1/models.

    Returns a dict that may contain 'ctx_size' and/or 'genmax' keys.
    On any failure, returns an empty dict.
    """
    headers = _auth_headers(api_key)
    result: dict[str, Any] = {}
    try:
        resp = httpx.get(f"{base_url}/v1/models", headers=headers, timeout=10.0)
        if resp.status_code != 200:
            return result
        data = resp.json()
        models = data.get("data", [])
        if not models:
            return result
        # Use the first model's metadata (or the only one).
        meta = models[0]
        # Different backends expose this differently.
        for key in ("context_length", "max_model_len", "context_window"):
            if key in meta:
                result["ctx_size"] = int(meta[key])
                break
        for key in ("max_tokens", "max_completion_tokens", "max_generation_len"):
            if key in meta:
                result["genmax"] = int(meta[key])
                break
        # Grab the model id if available.
        if "id" in meta:
            result["model_id"] = meta["id"]
    except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError):
        pass
    return result


def list_models(base_url: str, api_key: str | None) -> list[str]:
    """Fetch available models from /v1/models."""
    headers = _auth_headers(api_key)
    try:
        resp = httpx.get(f"{base_url}/v1/models", headers=headers, timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            models = data.get("data", [])
            return [m.get("id") for m in models if "id" in m]
    except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError):
        pass
    return []


def stream_chat(
    base_url: str,
    api_key: str | None,
    model: str,
    messages: list[dict[str, Any]],
    samplers: dict[str, Any],
    genmax: int,
    debug: bool = False,
) -> Generator[tuple[Literal["delta", "error", "usage"], str], None, None]:
    """Stream a chat completion. Yields structured events.

    On connection/API errors, yields a single event ("error", message) so the
    caller can display it. Otherwise yields ("delta", text) events and, if the
    server reports it, a final ("usage", json) event carrying token counts.
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        # Ask OpenAI-compatible servers to include a usage object in the final
        # chunk. Servers that don't support it simply ignore the field.
        "stream_options": {"include_usage": True},
    }
    if genmax > 0:
        payload["max_tokens"] = genmax
    # Merge samplers (arbitrary nested dicts) directly into the payload.
    payload.update(samplers)

    if debug:
        print("\n── DEBUG: request payload ──")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        print("── end payload ──\n")

    headers = _auth_headers(api_key)
    client = get_client(base_url, api_key)

    try:
        with client.stream(
            "POST",
            f"{base_url}/v1/chat/completions",
            json=payload,
            headers=headers,
        ) as response:
            if response.status_code != 200:
                # Try to read an error body.
                body = response.read().decode(errors="replace")
                yield "error", f"HTTP {response.status_code}: {body}"
                return

            for line in response.iter_lines():
                if not line:
                    continue
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data_str)
                        # Some providers (e.g. NVIDIA NIM) return HTTP 200 but
                        # stream the error in-band as a data: line. Surface it
                        # instead of silently yielding a blank response.
                        err = chunk.get("error")
                        if err:
                            msg = err.get("message") if isinstance(err, dict) else str(err)
                            yield "error", f"Server error: {msg}"
                            return
                        usage = chunk.get("usage")
                        if usage:
                            yield "usage", json.dumps(usage)
                        choices = chunk.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {}).get("content", "")
                            if delta:
                                yield "delta", delta
                    except (json.JSONDecodeError, IndexError):
                        continue
    except httpx.ConnectError as e:
        yield "error", f"Connection failed: {e}"
    except httpx.HTTPError as e:
        yield "error", f"HTTP error: {e}"
    except Exception as e:
        yield "error", f"Unexpected error: {e}"


def _auth_headers(api_key: str | None) -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers
