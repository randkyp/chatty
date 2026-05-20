"""
Chat session: message history, token counting, and context clipping.

Token counting tries the server's /tokenize endpoint first (llama.cpp style),
then falls back to tiktoken's cl100k_base.  Context clipping removes the
oldest user/assistant pairs until the history fits within ctx_size - genmax.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import tiktoken


# ── Token counting ─────────────────────────────────────────────────────────

@dataclass
class TokenCounter:
    """Counts tokens, preferring the server's /tokenize endpoint."""

    base_url: str
    _use_server: bool | None = field(default=None, repr=False)
    _tiktoken_enc: Any = field(default=None, repr=False)

    def _get_tiktoken(self) -> Any:
        if self._tiktoken_enc is None:
            self._tiktoken_enc = tiktoken.get_encoding("cl100k_base")
        return self._tiktoken_enc

    def count(self, text: str) -> int:
        """Return token count for *text*."""
        # First call: probe the server.
        if self._use_server is None:
            try:
                resp = httpx.post(
                    f"{self.base_url}/tokenize",
                    json={"content": text},
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    self._use_server = True
                    return len(data.get("tokens", []))
                else:
                    self._use_server = False
            except (httpx.HTTPError, Exception):
                self._use_server = False

        if self._use_server:
            try:
                resp = httpx.post(
                    f"{self.base_url}/tokenize",
                    json={"content": text},
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    return len(resp.json().get("tokens", []))
            except (httpx.HTTPError, Exception):
                pass
            # Fall through to tiktoken on transient failure.

        return len(self._get_tiktoken().encode(text))

    def count_messages(self, messages: list[dict[str, Any]]) -> int:
        """Approximate token count for a list of chat messages.

        Uses ~4 overhead tokens per message (role, separators) following
        the OpenAI convention.
        """
        total = 0
        for msg in messages:
            total += 4  # role + formatting overhead
            content = msg.get("content", "")
            if isinstance(content, str):
                total += self.count(content)
            elif isinstance(content, list):
                for part in content:
                    if part.get("type") == "text":
                        total += self.count(part.get("text", ""))
                    elif part.get("type") == "image_url":
                        total += 1000  # safe buffer image token cost
        total += 2  # reply priming
        return total


# ── Message history management ─────────────────────────────────────────────

@dataclass
class ChatSession:
    """Manages the message history and context window."""

    system_prompt: str | None = None
    ctx_size: int = 8192
    genmax: int = 1024
    messages: list[dict[str, Any]] = field(default_factory=list)
    pending_images: list[dict[str, Any]] = field(default_factory=list)
    _counter: TokenCounter | None = field(default=None, repr=False)

    def set_counter(self, counter: TokenCounter) -> None:
        self._counter = counter

    # ── History manipulation ───────────────────────────────────────────────

    def add_user_message(self, content: str, images: list[dict[str, Any]] | None = None) -> None:
        self._append("user", content, images)

    def add_assistant_message(self, content: str) -> None:
        self._append("assistant", content)

    def _append(self, role: str, content: str, images: list[dict[str, Any]] | None = None) -> None:
        """Append a message, concatenating if the previous has the same role."""
        if self.messages and self.messages[-1]["role"] == role:
            prev_msg = self.messages[-1]
            prev_content = prev_msg["content"]

            # Concatenate content.
            if isinstance(prev_content, str) and not images:
                prev_msg["content"] = prev_content + "\n" + content
            else:
                parts: list[dict[str, Any]] = []
                if isinstance(prev_content, str):
                    if prev_content:
                        parts.append({"type": "text", "text": prev_content})
                else:
                    parts.extend(prev_content)

                if content:
                    parts.append({"type": "text", "text": content})

                if images:
                    for img in images:
                        parts.append({
                            "type": "image_url",
                            "image_url": {"url": img["data_url"]},
                        })

                prev_msg["content"] = parts
        else:
            if images:
                parts: list[dict[str, Any]] = []
                if content:
                    parts.append({"type": "text", "text": content})
                for img in images:
                    parts.append({
                        "type": "image_url",
                        "image_url": {"url": img["data_url"]},
                    })
                self.messages.append({"role": role, "content": parts})
            else:
                self.messages.append({"role": role, "content": content})

    def clear(self) -> None:
        """Clear user/assistant history (system prompt is re-injected at send time)."""
        self.messages.clear()
        self.pending_images.clear()

    def undo(self) -> bool:
        """Remove the last assistant+user pair. Returns False if nothing to undo."""
        if not self.messages:
            return False
        # Remove trailing assistant message if present.
        if self.messages and self.messages[-1]["role"] == "assistant":
            self.messages.pop()
        # Remove trailing user message if present.
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages.pop()
            return True
        return True

    # ── Context clipping ───────────────────────────────────────────────────

    def build_payload_messages(self) -> list[dict[str, Any]] | None:
        """Build the message list for the API, clipped to fit the context window.

        Returns None if the system prompt + latest user message alone exceed
        the budget (caller should show a warning).
        """
        assert self._counter is not None, "TokenCounter not set"
        budget = self.ctx_size - self.genmax

        # Start with optional system message.
        prefix: list[dict[str, Any]] = []
        if self.system_prompt:
            prefix.append({"role": "system", "content": self.system_prompt})

        # We must always include the latest user message.
        if not self.messages:
            return prefix if prefix else []

        latest_user = self.messages[-1]

        # Check if system + latest user alone busts the budget.
        mandatory = prefix + [latest_user]
        if self._counter.count_messages(mandatory) > budget:
            return None  # signal: message too long

        # Build from newest to oldest, accumulating until we hit budget.
        # Work with a copy of non-system messages.
        history = list(self.messages)

        # Clip oldest pairs until it fits.
        while True:
            candidate = prefix + history
            # Ensure first non-system message is 'user', not 'assistant'.
            first_non_sys = 0 if not prefix else 1 if len(candidate) > 1 else 0
            if first_non_sys < len(candidate) and candidate[first_non_sys]["role"] == "assistant":
                # Drop the leading assistant message.
                history = history[1:]
                continue

            tokens = self._counter.count_messages(candidate)
            if tokens <= budget:
                return candidate
            # Drop the oldest message from history.
            if len(history) <= 1:
                # Only the latest message remains; if it still busts, give up.
                return None
            history = history[1:]

    def get_token_count(self) -> int:
        """Count tokens in current history (for display)."""
        if self._counter is None:
            return -1
        msgs: list[dict[str, Any]] = []
        if self.system_prompt:
            msgs.append({"role": "system", "content": self.system_prompt})
        msgs.extend(self.messages)
        return self._counter.count_messages(msgs)
