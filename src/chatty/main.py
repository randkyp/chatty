"""
Main entry point: wires together config, session, API, UI, and commands.

The main loop reads input, dispatches slash commands, and streams
API responses with Ctrl+C interrupt support.
"""

from __future__ import annotations

import sys

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
    print_warning,
    print_welcome,
    render_assistant_message,
    render_user_message,
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
    )

    prompt_session = create_prompt_session()

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
        render_user_message(text)
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
        console.print("[assistant]Assistant ›[/]", end=" ")

        try:
            for chunk in stream:
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

        # Save response (partial or complete) to history.
        full_response = "".join(collected)
        if full_response:
            session.add_assistant_message(full_response)
            # Show the rendered markdown version.
            render_assistant_message(full_response)

        # Display token usage.
        token_count = session.get_token_count()
        if token_count >= 0:
            budget = session.ctx_size - session.genmax
            print_system(f"[tokens: ~{token_count} / {budget} budget]")


if __name__ == "__main__":
    main()
