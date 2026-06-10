"""
Chat session: message history, token counting, and context clipping.

Token counting tries the server's /tokenize endpoint first (llama.cpp style),
then falls back to tiktoken's cl100k_base.  Context clipping removes the
oldest user/assistant pairs until the history fits within ctx_size - genmax.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import httpx
import tiktoken

from chatty.api import get_client

# ── Token counting ─────────────────────────────────────────────────────────


@dataclass
class TokenCounter:
    """Counts tokens, preferring the server's /tokenize endpoint."""

    base_url: str
    _use_server: bool | None = field(default=None, repr=False)
    _tiktoken_enc: Any = field(default=None, repr=False)
    _cache: OrderedDict[tuple[bool | None, str], int] = field(default_factory=OrderedDict, repr=False)

    def _get_tiktoken(self) -> Any:
        if self._tiktoken_enc is None:
            self._tiktoken_enc = tiktoken.get_encoding("cl100k_base")
        return self._tiktoken_enc

    def _post_tokenize(self, text: str) -> httpx.Response:
        """POST to the server's /tokenize endpoint, reusing a pooled client."""
        return get_client(self.base_url, None).post(
            f"{self.base_url}/tokenize",
            json={"content": text},
            timeout=5.0,
        )

    def count(self, text: str) -> int:
        """Return token count for *text*."""
        if not text:
            return 0
        cache_key = (self._use_server, text)
        if cache_key in self._cache:
            self._cache.move_to_end(cache_key)
            return self._cache[cache_key]

        # First call: probe the server.
        if self._use_server is None:
            try:
                resp = self._post_tokenize(text)
                if resp.status_code == 200:
                    data = resp.json()
                    self._use_server = True
                    ans = len(data.get("tokens", []))
                    self._cache[cache_key] = ans
                    self._enforce_cache_size()
                    return ans
                else:
                    self._use_server = False
            except (httpx.HTTPError, json.JSONDecodeError):
                self._use_server = False

        if self._use_server:
            try:
                resp = self._post_tokenize(text)
                if resp.status_code == 200:
                    ans = len(resp.json().get("tokens", []))
                    self._cache[cache_key] = ans
                    self._enforce_cache_size()
                    return ans
            except (httpx.HTTPError, json.JSONDecodeError):
                pass
            # Fall through to tiktoken on transient failure.

        ans = len(self._get_tiktoken().encode(text))
        self._cache[cache_key] = ans
        self._enforce_cache_size()
        return ans

    def _enforce_cache_size(self) -> None:
        while len(self._cache) > 1024:
            self._cache.popitem(last=False)

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
    genmax: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    pending_images: list[dict[str, Any]] = field(default_factory=list)
    _counter: TokenCounter | None = field(default=None, repr=False)
    # Revert token for the most recent add_user_message (merged?, original_content).
    _last_user_add: tuple[bool, Any] | None = field(default=None, repr=False)

    @property
    def context_budget(self) -> int:
        """Return the effective context budget after reserving room for the response."""
        if self.genmax <= 0:
            return self.ctx_size - (self.ctx_size // 10)
        return self.ctx_size - self.genmax

    def set_counter(self, counter: TokenCounter) -> None:
        self._counter = counter

    # ── History manipulation ───────────────────────────────────────────────

    def add_user_message(self, content: str, images: list[dict[str, Any]] | None = None) -> None:
        # Record exactly how this add mutated history so it can be reverted
        # precisely (a plain undo() would discard earlier merged-in user text).
        self._last_user_add = self._append("user", content, images)

    def add_assistant_message(self, content: str) -> None:
        self._append("assistant", content)
        self._last_user_add = None

    def revert_last_user_message(self) -> None:
        """Revert exactly the most recent add_user_message (merge-aware).

        Used when a send fails before any assistant text arrives. Unlike undo(),
        this restores merged-in earlier user content instead of dropping it.
        """
        token = self._last_user_add
        self._last_user_add = None
        if token is None:
            return
        merged, original = token
        if merged:
            if self.messages:
                self.messages[-1]["content"] = original
        elif self.messages:
            self.messages.pop()

    def _append(self, role: str, content: str, images: list[dict[str, Any]] | None = None) -> tuple[bool, Any]:
        """Append a message, concatenating if the previous has the same role.

        Returns (merged, original_content): *merged* is True when the message was
        folded into the previous same-role message, and *original_content* is that
        message's content from before the merge (for precise reverting).
        """
        if self.messages and self.messages[-1]["role"] == role:
            prev_msg = self.messages[-1]
            prev_content = prev_msg["content"]
            # prev_content is never mutated in place below (we always rebind
            # prev_msg["content"]), so it is safe to keep as the revert original.

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
                        parts.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": img["data_url"]},
                            }
                        )

                prev_msg["content"] = parts
            return True, prev_content
        else:
            if images:
                parts: list[dict[str, Any]] = []
                if content:
                    parts.append({"type": "text", "text": content})
                for img in images:
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": img["data_url"]},
                        }
                    )
                self.messages.append({"role": role, "content": parts})
            else:
                self.messages.append({"role": role, "content": content})
            return False, None

    def stage_user_message(
        self, text: str, extra_images: list[dict[str, Any]] | None = None
    ) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
        """Prepare a user turn: extract @image paths, attach pending/extra images,
        append the message, and build the clipped API payload.

        Returns (payload, attached_images). *payload* is None when the message
        busts the context budget; the caller should warn and then call
        revert_last_user_message(). Shared by the CLI and web server.
        """
        from chatty.images import extract_images_from_text

        text, text_images = extract_images_from_text(text)
        attached = list(self.pending_images) + text_images + list(extra_images or [])
        self.add_user_message(text, images=attached)
        self.pending_images.clear()
        payload = self.build_payload_messages()
        return payload, attached

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
        budget = self.context_budget

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
        mandatory_tokens = self._counter.count_messages(mandatory)
        mandatory_padding = max(64, len(mandatory) * 3)
        if mandatory_tokens + mandatory_padding > budget:
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
            padding = max(64, len(candidate) * 3)
            if tokens + padding <= budget:
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

    def get_padding_count(self) -> int:
        """Count control token padding for current history (for display)."""
        num_msgs = len(self.messages)
        if self.system_prompt:
            num_msgs += 1
        return max(64, num_msgs * 3)
