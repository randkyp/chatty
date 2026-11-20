"""Per-request timing capture, classification, and display.

``RequestTimings`` collects monotonic stamps while a request streams. The
displayed stages are exclusive, so their durations add up to the total:

    app     submit -> request dispatched (token counting, payload build)
    net     dispatch -> response headers
    prompt  headers -> first token
    thinking  first reasoning token -> first content token (when streamed)
    output  first content token -> last content token

TTFT is a roll-up of ``net + prompt``, not a stage of its own, so it never
double-counts. A server that buffers its response sends headers only with the
first token; that case is labelled ``net/queue`` and the ``prompt`` stage is
``n/a`` because the client cannot separate waiting from compute.

Provider-reported numbers are printed as-is. Values derived from streamed text
are marked ``~`` (token counts) or ``(est.)`` (an inferred attribution).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Gaps smaller than this mean the server held the headers until the first token.
BUFFERED_THRESHOLD = 0.05
# Below this a decode span is measurement noise, not a rate.
MIN_MEASURABLE_SPAN = 0.001
# Cheap offline approximation; reasoning counts are always labelled as estimates.
CHARS_PER_TOKEN = 4


def _as_int(value: Any) -> int | None:
    """Return *value* as an int when it plausibly is one, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


@dataclass
class RequestTimings:
    """Monotonic stamps and usage data for a single completed request."""

    submitted_at: float = field(default_factory=time.monotonic)
    clock: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))
    dispatched_at: float | None = None
    headers_at: float | None = None
    first_reasoning_at: float | None = None
    first_content_at: float | None = None
    last_content_at: float | None = None
    finished_at: float | None = None
    usage: dict[str, Any] | None = None
    server_timings: dict[str, Any] = field(default_factory=dict)
    server_prompt_seconds: float | None = None
    reasoning_chars: int = 0
    interrupted: bool = False
    error: str | None = None

    # ── Capture ────────────────────────────────────────────────────────────

    def mark_dispatched(self) -> None:
        if self.dispatched_at is None:
            self.dispatched_at = time.monotonic()

    def mark_headers(self, headers: Any) -> None:
        if self.headers_at is None:
            self.headers_at = time.monotonic()
        processing_ms = headers.get("openai-processing-ms") if headers is not None else None
        if self.server_prompt_seconds is None:
            try:
                self.server_prompt_seconds = float(processing_ms) / 1000 if processing_ms else None
            except (TypeError, ValueError):
                self.server_prompt_seconds = None

    def observe(self, event_type: str, content: str) -> None:
        """Fold one stream event into the stamps."""
        now = time.monotonic()
        if event_type == "reasoning":
            if self.first_reasoning_at is None:
                self.first_reasoning_at = now
            self.reasoning_chars += len(content)
        elif event_type == "delta":
            if self.first_content_at is None:
                self.first_content_at = now
            self.last_content_at = now
        elif event_type == "usage":
            try:
                self.usage = json.loads(content)
            except json.JSONDecodeError:
                self.usage = None
        elif event_type == "timings":
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                return
            if isinstance(data, dict):
                self.server_timings = data
                prompt_ms = data.get("prompt_ms")
                if self.server_prompt_seconds is None and isinstance(prompt_ms, (int, float)):
                    self.server_prompt_seconds = float(prompt_ms) / 1000

    def finish(self, *, usage: dict[str, Any] | None = None, interrupted: bool = False) -> None:
        if self.finished_at is None:
            self.finished_at = time.monotonic()
        if usage is not None:
            self.usage = usage
        self.interrupted = self.interrupted or interrupted

    # ── Derived stages ─────────────────────────────────────────────────────

    @property
    def first_token_at(self) -> float | None:
        stamps = [t for t in (self.first_reasoning_at, self.first_content_at) if t is not None]
        return min(stamps) if stamps else None

    @property
    def total(self) -> float | None:
        return None if self.finished_at is None else self.finished_at - self.submitted_at

    @property
    def app_duration(self) -> float | None:
        return None if self.dispatched_at is None else self.dispatched_at - self.submitted_at

    @property
    def net_duration(self) -> float | None:
        if self.dispatched_at is None or self.headers_at is None:
            return None
        return self.headers_at - self.dispatched_at

    @property
    def headers_carry_first_token(self) -> bool:
        """True when headers arrived with the first token, hiding server waiting."""
        if self.server_prompt_seconds is not None or self.headers_at is None:
            return False
        first = self.first_token_at
        return first is not None and (first - self.headers_at) < BUFFERED_THRESHOLD

    @property
    def prompt_duration(self) -> float | None:
        if self.server_prompt_seconds is not None:
            return self.server_prompt_seconds
        if self.headers_at is None or self.first_token_at is None:
            return None
        if self.headers_carry_first_token:
            return None
        return self.first_token_at - self.headers_at

    @property
    def ttft(self) -> float | None:
        first = self.first_token_at
        if first is None or self.dispatched_at is None:
            return None
        return first - self.dispatched_at

    @property
    def thinking_duration(self) -> float | None:
        if self.first_reasoning_at is None or self.first_content_at is None:
            return None
        if self.first_content_at < self.first_reasoning_at:
            return None
        return self.first_content_at - self.first_reasoning_at

    @property
    def output_duration(self) -> float | None:
        if self.first_content_at is None or self.last_content_at is None:
            return None
        if self.last_content_at <= self.first_content_at:
            return None
        return self.last_content_at - self.first_content_at

    @property
    def reported_reasoning_tokens(self) -> int | None:
        details = (self.usage or {}).get("completion_tokens_details")
        return _as_int(details.get("reasoning_tokens")) if isinstance(details, dict) else None

    @property
    def reasoning_token_estimate(self) -> int | None:
        if self.reasoning_chars <= 0:
            return None
        return max(1, round(self.reasoning_chars / CHARS_PER_TOKEN))

    @property
    def silent_reasoning(self) -> bool:
        """The provider reported thinking but streamed no reasoning text."""
        reported = self.reported_reasoning_tokens
        return bool(reported) and self.first_reasoning_at is None

    @property
    def split(self) -> TokenSplit:
        return build_split(self.usage, self.reasoning_token_estimate)

    @property
    def decode_rate(self) -> float | None:
        """Completion tokens per second of decode, excluding prompt and render."""
        span = self.last_content_at
        first = self.first_token_at
        completion = _as_int((self.usage or {}).get("completion_tokens"))
        if span is None or first is None or not completion:
            return None
        duration = span - first
        if duration < MIN_MEASURABLE_SPAN:
            return None
        return completion / duration


@dataclass(frozen=True)
class TokenSplit:
    """Completion tokens split into reasoning and visible message tokens."""

    prompt: int | None
    completion: int | None
    output: int | None = None
    reasoning: int | None = None
    reasoning_estimated: bool = False
    # None when the provider did not tell us whether reasoning is inside completion.
    reasoning_included: bool | None = None


def build_split(usage: dict[str, Any] | None, reasoning_estimate: int | None) -> TokenSplit:
    """Build a split without ever rewriting a provider-reported number.

    Canonical OpenAI usage counts reasoning inside ``completion_tokens``. When
    only the count is available we assume that convention, but a reasoning
    count larger than the completion count proves the provider excludes it, so
    the assumption is dropped rather than clamped.
    """
    if not usage:
        return TokenSplit(None, None)
    prompt = _as_int(usage.get("prompt_tokens"))
    completion = _as_int(usage.get("completion_tokens"))
    details = usage.get("completion_tokens_details")
    reported = details.get("reasoning_tokens") if isinstance(details, dict) else None
    reasoning = _as_int(reported)
    estimated = False
    if reasoning is None and reasoning_estimate:
        reasoning, estimated = reasoning_estimate, True
    if reasoning is None:
        return TokenSplit(prompt, completion)
    if completion is None:
        return TokenSplit(prompt, None, None, reasoning, estimated, None)
    if reasoning > completion:
        return TokenSplit(prompt, completion, completion, reasoning, estimated, False)
    return TokenSplit(prompt, completion, completion - reasoning, reasoning, estimated, True)


# ── Formatting ─────────────────────────────────────────────────────────────


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    millis = seconds * 1000
    if millis < 1:
        return "<1ms"
    if seconds < 1:
        return f"{millis:.0f}ms"
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes, rest = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m {rest}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def format_elapsed(seconds: float | None) -> str:
    """Coarse duration for the compact status line."""
    if seconds is None:
        return "n/a"
    rounded = max(0, round(seconds))
    if rounded < 60:
        return f"{rounded}s"
    minutes, rest = divmod(rounded, 60)
    if minutes < 60:
        return f"{minutes}m" if rest == 0 else f"{minutes}m {rest}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def format_timing_line(record: RequestTimings) -> str:
    """One line: every stage, in pipeline order."""
    net_label = "net/queue" if record.headers_carry_first_token else "net"
    parts = [
        f"app {format_duration(record.app_duration)}",
        f"{net_label} {format_duration(record.net_duration)}",
    ]
    if record.silent_reasoning:
        # The first-token gap contains unobservable thinking; naming it prompt
        # processing would claim knowledge the client does not have.
        parts.append(f"prompt+thinking (est.) {format_duration(record.ttft)}")
    else:
        parts.append(f"prompt {format_duration(record.prompt_duration)}")
    parts.append(f"ttft {format_duration(record.ttft)}")
    if record.thinking_duration is not None:
        parts.append(f"thinking {format_duration(record.thinking_duration)}")
    parts.append(f"output {format_duration(record.output_duration)}")
    parts.append(f"total {format_duration(record.total)}")

    line = f"{record.clock} " + " → ".join(parts)
    flags = [name for name, set_ in (("interrupted", record.interrupted), ("error", record.error)) if set_]
    return f"{line} · {' · '.join(flags)}" if flags else line


def format_footer(record: RequestTimings) -> str | None:
    """Second line: usage detail that does not belong in the pipeline."""
    split = record.split
    usage = record.usage or {}
    parts: list[str] = []
    if split.completion is not None:
        parts.append(f"out {split.completion:,}")
    if split.reasoning is not None:
        mark = "~" if split.reasoning_estimated else ""
        included = " (out incl.)" if split.reasoning_included else ""
        parts.append(f"reasoning {mark}{split.reasoning:,}{included}")

    details = usage.get("prompt_tokens_details")
    cached = _as_int(details.get("cached_tokens")) if isinstance(details, dict) else None
    if cached is not None:
        if split.prompt:
            parts.append(f"cached {cached:,} ({cached / split.prompt * 100:.0f}% hit)")
        else:
            parts.append(f"cached {cached:,}")

    rate = record.decode_rate
    if rate is not None:
        parts.append(f"{rate:.1f} tok/s")

    cost = usage.get("cost")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0:
        parts.append(_format_cost(float(cost)))

    return " · ".join(parts) if parts else None


def format_timing_report(record: RequestTimings) -> list[str]:
    """The full ``/timing`` output: pipeline line plus optional footer."""
    footer = format_footer(record)
    return [format_timing_line(record)] + ([footer] if footer else [])


def format_one_liner(record: RequestTimings) -> str:
    """The always-on post-response status line."""
    split = record.split
    total = _as_int((record.usage or {}).get("total_tokens"))
    if total is None and split.prompt is not None and split.completion is not None:
        total = split.prompt + split.completion
    total = total or 0

    if split.prompt is not None and split.completion is not None:
        detail = ""
        if split.reasoning is not None and split.output is not None:
            mark = "~" if split.reasoning_estimated else ""
            detail = f": {mark}{split.reasoning:,} rea + {split.output:,} mes"
        body = f"{total:,} ({split.prompt:,} in + {split.completion:,} out{detail})"
    else:
        body = f"{total:,}"

    rate = record.decode_rate
    rate_text = f", {rate:.1f} tok/s" if rate is not None else ""
    return f"[{record.clock} tokens: {body}{rate_text}, {format_elapsed(record.total)}]"


def _format_cost(cost: float) -> str:
    if cost >= 1:
        return f"${cost:.2f}"
    if cost >= 0.01:
        return f"${cost:.3f}"
    return f"${cost:.4f}"
