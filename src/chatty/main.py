"""
Main entry point: wires together config, session, API, UI, and commands.

The main loop reads input, dispatches slash commands, and streams
API responses with Ctrl+C interrupt support.
"""

from __future__ import annotations

import itertools
import os
import sys

from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from chatty.api import fetch_model_metadata, stream_chat
from chatty.chat_session import ChatSession, TokenCounter
from chatty.commands import handle_command
from chatty.config import AppConfig, load_config
from chatty.ui import (
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


def _resolve_limits(cfg: AppConfig) -> None:
    """Fill in ctx_size / genmax from server metadata if not set in profile."""
    p = cfg.profile
    if p.ctx_size and p.genmax and p.model:
        return  # Everything already set, skip network call.

    meta = fetch_model_metadata(p.base_url, p.api_key)

    if not p.ctx_size:
        p.ctx_size = meta.get("ctx_size", 8192)
    if not p.genmax:
        p.genmax = meta.get("genmax", 1024)
    if not p.model:
        p.model = meta.get("model_id", "default")


def main(argv: list[str] | None = None) -> None:
    cfg = load_config(argv)
    _resolve_limits(cfg)

    session = ChatSession(
        system_prompt=cfg.profile.system_prompt,
        ctx_size=cfg.profile.ctx_size or 8192,
        genmax=cfg.profile.genmax or 1024,
    )
    counter = TokenCounter(base_url=cfg.profile.base_url)
    session.set_counter(counter)

    print_welcome(
        cfg.profile.name,
        cfg.profile.model or "(auto)",
        session.ctx_size,
        session.genmax,
        enter_sends=cfg.enter_sends,
    )

    prompt_session = create_prompt_session(enter_sends=cfg.enter_sends)

    while True:
        try:
            raw = get_user_input(prompt_session)
        except KeyboardInterrupt:
            continue

        if raw is None:
            # Ctrl-D → exit
            print_system("Goodbye!")
            break

        text = raw.strip()
        if not text:
            continue

        print_user(text)

        # ── Slash command handling ──────────────────────────────────────
        if text.startswith("/") and not text.startswith("//"):
            result = handle_command(text, session, cfg)
            if result.quit:
                print_system("Goodbye!")
                break
            if result.message:
                print_system(result.message)
            continue

        # ── Escape: // → literal / ────────────────────────────────────
        if text.startswith("//"):
            text = text[1:]  # strip one leading slash

        # ── Send message to API ────────────────────────────────────────
        session.add_user_message(text)

        payload_messages = session.build_payload_messages()
        if payload_messages is None:
            print_warning(
                "Message + system prompt exceeds context budget "
                f"({session.ctx_size} - {session.genmax} = {session.ctx_size - session.genmax} tokens). "
                "Try /clear or /ctx to increase the limit."
            )
            # Remove the user message we just added since we can't send it.
            session.undo()
            continue

        model = cfg.profile.model or "default"
        stream = stream_chat(
            base_url=cfg.profile.base_url,
            api_key=cfg.profile.api_key,
            model=model,
            messages=payload_messages,
            samplers=cfg.profile.samplers,
            genmax=session.genmax,
            debug=cfg.debug,
        )

        # Stream response with Ctrl-C interrupt support.
        collected: list[str] = []
        interrupted = False

        # Print a header for the streaming response.
        console.print()

        stream_iter = iter(stream)
        try:
            with Live(
                Text.from_markup("[assistant]Assistant ›[/] [dim italic]Thinking...[/]"),
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

        console.print("[assistant]Assistant ›[/]", end="" if cfg.raw_output else "\n")

        if interrupted:
            if cfg.raw_output:
                print_assistant_done()
            print_warning("Generation interrupted.")
        else:
            if cfg.raw_output:
                # Raw mode: print chunks directly (natural terminal scrolling).
                try:
                    for chunk in stream_iter:
                        if chunk.startswith("[error]"):
                            print_error(chunk)
                            break
                        print_assistant_chunk(chunk)
                        collected.append(chunk)
                except KeyboardInterrupt:
                    interrupted = True
                    print_assistant_done()
                    print_warning("Generation interrupted.")

                if not interrupted:
                    print_assistant_done()
            else:
                # Markdown mode: keep a small tail-preview in Live during
                # streaming, then snap to rendered Markdown on completion.
                try:
                    term_h = os.get_terminal_size().lines
                except OSError:
                    term_h = 24
                
                preview_lines = max(term_h // 2, 6)

                def _tail(text: str) -> Text:
                    """Return a constant-height, non-wrapping preview."""
                    lines = text.splitlines()
                    if len(lines) < preview_lines:
                        # Pad to ensure constant height and prevent scrolling
                        lines = lines + [""] * (preview_lines - len(lines))
                    else:
                        lines = lines[-preview_lines:]
                        if lines:
                            lines[0] = "…"
                    
                    # no_wrap=True prevents wrapped lines from increasing height
                    return Text("\n".join(lines), no_wrap=True, overflow="ellipsis")

                try:
                    with Live(
                        _tail(""),
                        console=console,
                        refresh_per_second=8,
                        transient=True,
                    ) as live:
                        for chunk in stream_iter:
                            if chunk.startswith("[error]"):
                                print_error(chunk)
                                break
                            collected.append(chunk)
                            live.update(_tail("".join(collected)))
                except KeyboardInterrupt:
                    interrupted = True

                # Render the full Markdown outside the Live context so it
                # prints via normal terminal scrolling (no duplication).
                if collected:
                    console.print(Markdown("".join(collected)))
                
                if interrupted:
                    print_warning("Generation interrupted.")

        # Save response (partial or complete) to history.
        full_response = "".join(collected)
        if full_response:
            session.add_assistant_message(full_response)

        # Display token usage.
        token_count = session.get_token_count()
        if token_count >= 0:
            budget = session.ctx_size - session.genmax
            print_system(f"[tokens: ~{token_count} / {budget} budget]")


if __name__ == "__main__":
    main()
