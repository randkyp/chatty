"""
Main entry point: wires together config, session, API, UI, and commands.

The main loop reads input, dispatches slash commands, and streams
API responses with Ctrl+C interrupt support.
"""

from __future__ import annotations

import itertools
import json
import shutil
import sys
import time
from collections.abc import Iterator
from pathlib import Path

from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from chatty.api import close_clients, stream_chat
from chatty.chat_session import ChatSession, TokenCounter
from chatty.clipboard import ClipboardError, copy_text
from chatty.commands import CommandResult, handle_command, is_command, save_session
from chatty.config import AppConfig, ProfileNotFoundError, load_config, parse_args, resolve_limits
from chatty.ui import (
    ChattyCompleter,
    console,
    create_prompt_session,
    get_user_input,
    print_assistant_chunk,
    print_assistant_done,
    print_error,
    print_system,
    print_user,
    print_warning,
    print_welcome,
)

# ── Streaming + rendering ──────────────────────────────────────────────────


def stream_and_render(
    cfg: AppConfig,
    messages: list[dict],
    genmax: int,
    *,
    label: str = "Assistant",
) -> tuple[str, bool, dict | None]:
    """Stream a completion and render it to the terminal.

    Handles the Thinking spinner, the rolling tail preview (or raw output),
    Ctrl+C interruption, and final Markdown rendering. Returns
    (full_response, interrupted, usage) where *usage* is the server-reported
    token usage dict if available.
    """
    stream = stream_chat(
        base_url=cfg.profile.base_url,
        api_key=cfg.profile.api_key,
        model=cfg.profile.model or "default",
        messages=messages,
        samplers=cfg.profile.samplers,
        genmax=genmax,
        debug=cfg.debug,
    )

    collected: list[str] = []
    usage: dict | None = None
    interrupted = False

    console.print()
    stream_iter: Iterator = iter(stream)

    # Show a "Thinking..." spinner until the first event arrives.
    try:
        with Live(
            Text.from_markup(f"[assistant]{label} ›[/] [dim italic]Thinking...[/]"),
            console=console,
            transient=True,
            refresh_per_second=4,
        ):
            first_chunk = next(stream_iter)
        stream_iter = itertools.chain([first_chunk], stream_iter)
    except StopIteration:
        stream_iter = iter([])
    except KeyboardInterrupt:
        interrupted = True

    console.print(f"[assistant]{label} ›[/]", end="" if cfg.raw_output else "\n")

    def consume(handle_chunk) -> None:
        nonlocal usage, interrupted
        for event_type, content in stream_iter:
            if event_type == "error":
                print_error(content)
                break
            if event_type == "usage":
                try:
                    usage = json.loads(content)
                except json.JSONDecodeError:
                    usage = None
                continue
            collected.append(content)
            handle_chunk("".join(collected) if not cfg.raw_output else content)

    if not interrupted:
        try:
            if cfg.raw_output:
                consume(lambda chunk: print_assistant_chunk(chunk))
            else:
                term_h = shutil.get_terminal_size((80, 24)).lines
                preview_lines = max(term_h // 2, 6)

                def _tail(text: str) -> Text:
                    """Return a constant-height, non-wrapping preview."""
                    lines = text.splitlines()[-preview_lines:]
                    if len(lines) == preview_lines and lines:
                        lines[0] = "…"
                    lines += [""] * (preview_lines - len(lines))
                    return Text("\n".join(lines), no_wrap=True, overflow="ellipsis")

                with Live(_tail(""), console=console, refresh_per_second=8, transient=True) as live:
                    consume(lambda full: live.update(_tail(full)))
        except KeyboardInterrupt:
            interrupted = True

    if cfg.raw_output:
        print_assistant_done()
    elif collected:
        console.print(Markdown("".join(collected)))

    if interrupted:
        print_warning("Generation interrupted.")

    return "".join(collected), interrupted, usage


def _report_tokens(session: ChatSession, usage: dict | None, elapsed: float) -> None:
    """Display token usage, preferring the server's real counts over an estimate."""
    if usage and usage.get("completion_tokens") is not None:
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        total = usage.get("total_tokens", prompt + completion)
        rate = f", {completion / elapsed:.1f} tok/s" if elapsed > 0 and completion else ""
        print_system(f"[tokens: {total} ({prompt} ctx + {completion} gen){rate}]")
        return
    token_count = session.get_token_count()
    if token_count >= 0:
        print_system(f"[tokens: ~{token_count} / {session.context_budget} budget]")


# ── Slash-command result handling ──────────────────────────────────────────


def _handle_command_result(result: CommandResult, session: ChatSession, cfg: AppConfig) -> bool:
    """Act on a CommandResult. Returns True if the app should quit."""
    if result.quit:
        return True

    if result.ephemeral_prompt:
        messages = [{"role": "user", "content": result.ephemeral_prompt}]
        stream_and_render(cfg, messages, session.genmax, label="Assistant (ephemeral)")
        return False

    if result.resend_user:
        _send_and_render(session, cfg)
        return False

    if result.copy_last:
        last = _last_assistant_text(session)
        if not last:
            print_system("No assistant response to copy.")
        else:
            try:
                copy_text(last)
                print_system("Last response copied to clipboard.")
            except ClipboardError as e:
                print_error(f"Failed to copy to clipboard: {e}")
        return False

    if result.message:
        print_system(result.message)
    return False


def _last_assistant_text(session: ChatSession) -> str | None:
    for msg in reversed(session.messages):
        if msg.get("role") == "assistant":
            content = msg.get("content")
            return content if isinstance(content, str) else None
    return None


def _budget_warning(session: ChatSession) -> None:
    reserve = session.genmax if session.genmax > 0 else (session.ctx_size // 10)
    print_warning(
        "Message + system prompt exceeds context budget "
        f"({session.ctx_size} - {reserve} = {session.context_budget} tokens). "
        "Try /clear or /ctx to increase the limit."
    )


def _stream_record_report(session: ChatSession, cfg: AppConfig, payload_messages: list[dict]) -> None:
    """Stream a response for *payload_messages*, record it, and report tokens."""
    start = time.monotonic()
    full_response, _interrupted, usage = stream_and_render(cfg, payload_messages, session.genmax)
    elapsed = time.monotonic() - start

    if full_response:
        session.add_assistant_message(full_response)
    else:
        # Nothing generated (failed/interrupted before any text): drop the user
        # message so it isn't duplicated on retry. revert is merge-aware.
        session.revert_last_user_message()

    _report_tokens(session, usage, elapsed)


def _send_and_render(session: ChatSession, cfg: AppConfig) -> None:
    """Build the clipped payload from existing history, stream, and record (/retry)."""
    payload_messages = session.build_payload_messages()
    if payload_messages is None:
        _budget_warning(session)
        return
    _stream_record_report(session, cfg, payload_messages)


# ── Main loop ──────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.web:
        try:
            from chatty.web.server import run_server
        except ImportError:
            print(
                "[error] The web UI requires extra dependencies. Install them with:\n"
                "    uv tool install 'chatty[web]'   (or  pip install 'chatty[web]')",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        run_server(argv, host=args.host, port=args.port)
        return

    try:
        cfg = load_config(argv)
    except ProfileNotFoundError as e:
        print(f"[error] {e}", file=sys.stderr)
        raise SystemExit(1) from None
    resolve_limits(cfg)

    session = ChatSession(
        system_prompt=cfg.profile.system_prompt,
        ctx_size=cfg.profile.ctx_size if cfg.profile.ctx_size is not None else 8192,
        genmax=cfg.profile.genmax if cfg.profile.genmax is not None else 0,
    )
    session.set_counter(TokenCounter(base_url=cfg.profile.base_url))

    print_welcome(
        cfg.profile.name,
        cfg.profile.model or "(auto)",
        session.ctx_size,
        session.genmax,
        enter_sends=cfg.enter_sends,
    )

    prompt_session = create_prompt_session(
        enter_sends=cfg.enter_sends,
        completer=ChattyCompleter(cfg),
    )

    try:
        while True:
            try:
                raw = get_user_input(prompt_session)
            except KeyboardInterrupt:
                continue

            if raw is None:
                print_system("Goodbye!")
                break

            text = raw.strip()
            if not text:
                continue

            print_user(text)

            # ── Slash command handling ──────────────────────────────────────
            if is_command(text):
                result = handle_command(text, session, cfg)
                if _handle_command_result(result, session, cfg):
                    print_system("Goodbye!")
                    break
                continue

            # ── Escape: // → literal / ──────────────────────────────────────
            if text.startswith("//"):
                text = text[1:]

            # ── Send message to API ─────────────────────────────────────────
            payload_messages, attached = session.stage_user_message(text)
            for img in attached:
                if img.get("path"):
                    print_system(f" -> Attached image: {img['path']}")
                else:
                    print_system(" -> Attached image from clipboard")

            if payload_messages is None:
                _budget_warning(session)
                session.revert_last_user_message()
                continue

            _stream_record_report(session, cfg, payload_messages)
    finally:
        if cfg.autosave and session.messages:
            try:
                path = Path.home() / ".config" / "chatty" / "session.json"
                save_session(session, path)
                print_system(f"Session autosaved to {path}.")
            except OSError as e:
                print_error(f"Autosave failed: {e}")
        close_clients()


if __name__ == "__main__":
    main()
