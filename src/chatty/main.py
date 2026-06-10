"""
Main entry point: wires together config, session, API, UI, and commands.

The main loop reads input, dispatches slash commands, and streams
API responses with Ctrl+C interrupt support.
"""

from __future__ import annotations

import itertools
import re
import shutil
import subprocess
import sys

from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from chatty.api import fetch_model_metadata, stream_chat
from chatty.chat_session import ChatSession, TokenCounter
from chatty.commands import handle_command
from chatty.config import AppConfig, load_config
from chatty.images import extract_images_from_text
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

    if p.ctx_size is None:
        p.ctx_size = meta.get("ctx_size", 8192)
    if p.genmax is None:
        p.genmax = meta.get("genmax", 0)
    if not p.model:
        p.model = meta.get("model_id", "default")


def main(argv: list[str] | None = None) -> None:
    cfg = load_config(argv)
    _resolve_limits(cfg)

    session = ChatSession(
        system_prompt=cfg.profile.system_prompt,
        ctx_size=cfg.profile.ctx_size if cfg.profile.ctx_size is not None else 8192,
        genmax=cfg.profile.genmax if cfg.profile.genmax is not None else 0,
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
        first_word = text.split(None, 1)[0] if text else ""
        if text.startswith("/") and not text.startswith("//") and re.match(r"^/[a-zA-Z]+$", first_word):
            result = handle_command(text, session, cfg)
            if result.quit:
                print_system("Goodbye!")
                break

            if result.ephemeral_prompt:
                payload_messages = [{"role": "user", "content": result.ephemeral_prompt}]
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

                collected: list[str] = []
                interrupted = False

                console.print()
                stream_iter = iter(stream)

                try:
                    with Live(
                        Text.from_markup("[assistant]Assistant (ephemeral) ›[/] [dim italic]Thinking...[/]"),
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

                console.print("[assistant]Assistant (ephemeral) ›[/]", end="" if cfg.raw_output else "\n")

                if not interrupted:
                    try:
                        if cfg.raw_output:
                            for event_type, content in stream_iter:
                                if event_type == "error":
                                    print_error(content)
                                    break
                                print_assistant_chunk(content)
                                collected.append(content)
                        else:
                            term_h = shutil.get_terminal_size((80, 24)).lines
                            preview_lines = max(term_h // 2, 6)

                            def _tail_e(t: str) -> Text:
                                lines = t.splitlines()[-preview_lines:]
                                if len(lines) == preview_lines and lines:
                                    lines[0] = "…"
                                lines += [""] * (preview_lines - len(lines))
                                return Text("\n".join(lines), no_wrap=True, overflow="ellipsis")

                            with Live(_tail_e(""), console=console, refresh_per_second=8, transient=True) as live:
                                for event_type, content in stream_iter:
                                    if event_type == "error":
                                        print_error(content)
                                        break
                                    collected.append(content)
                                    live.update(_tail_e("".join(collected)))
                    except KeyboardInterrupt:
                        interrupted = True

                if cfg.raw_output:
                    print_assistant_done()
                elif collected:
                    console.print(Markdown("".join(collected)))

                if interrupted:
                    print_warning("Generation interrupted.")
                continue

            if result.copy_last:
                last_msg = None
                for msg in reversed(session.messages):
                    if msg.get("role") == "assistant":
                        last_msg = msg.get("content")
                        break

                if last_msg:
                    try:
                        import pyperclip

                        pyperclip.copy(last_msg)
                        print_system("Last response copied to clipboard.")
                    except ImportError:
                        try:
                            if sys.platform == "darwin":
                                subprocess.run(["pbcopy"], input=last_msg.encode("utf-8"), check=True)
                                print_system("Last response copied to clipboard.")
                            elif sys.platform.startswith("linux"):
                                if shutil.which("wl-copy"):
                                    subprocess.run(["wl-copy"], input=last_msg.encode("utf-8"), check=True)
                                    print_system("Last response copied to clipboard.")
                                elif shutil.which("xclip"):
                                    subprocess.run(
                                        ["xclip", "-selection", "clipboard", "-in"],
                                        input=last_msg.encode("utf-8"),
                                        check=True,
                                    )
                                    print_system("Last response copied to clipboard.")
                                else:
                                    print_warning(
                                        "No clipboard tool found (install wl-clipboard, xclip, or pyperclip)."
                                    )
                            elif sys.platform == "win32":
                                subprocess.run(["clip"], input=last_msg.encode("utf-16le"), check=True)
                                print_system("Last response copied to clipboard.")
                            else:
                                print_warning("Clipboard copying not supported on this OS without pyperclip.")
                        except Exception as e:
                            print_error(f"Failed to copy to clipboard: {e}")
                else:
                    print_system("No assistant response to copy.")
                continue

            if result.message:
                print_system(result.message)
            continue

        # ── Escape: // → literal / ────────────────────────────────────
        if text.startswith("//"):
            text = text[1:]  # strip one leading slash

        # ── Send message to API ────────────────────────────────────────
        text, text_images = extract_images_from_text(text)
        all_images = session.pending_images + text_images

        for img in all_images:
            if img.get("path"):
                print_system(f" -> Attached image: {img['path']}")
            else:
                print_system(" -> Attached image from clipboard")

        session.add_user_message(text, images=all_images)
        session.pending_images.clear()

        payload_messages = session.build_payload_messages()
        if payload_messages is None:
            reserve = session.genmax if session.genmax > 0 else (session.ctx_size // 10)
            print_warning(
                "Message + system prompt exceeds context budget "
                f"({session.ctx_size} - {reserve} = {session.context_budget} tokens). "
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

        if not interrupted:
            try:
                if cfg.raw_output:
                    for event_type, content in stream_iter:
                        if event_type == "error":
                            print_error(content)
                            break
                        print_assistant_chunk(content)
                        collected.append(content)
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
                        for event_type, content in stream_iter:
                            if event_type == "error":
                                print_error(content)
                                break
                            collected.append(content)
                            live.update(_tail("".join(collected)))
            except KeyboardInterrupt:
                interrupted = True

        if cfg.raw_output:
            print_assistant_done()
        elif collected:
            console.print(Markdown("".join(collected)))

        if interrupted:
            print_warning("Generation interrupted.")

        # Save response (partial or complete) to history.
        full_response = "".join(collected)
        if full_response:
            session.add_assistant_message(full_response)
        else:
            # If generation failed completely or was interrupted before yielding,
            # remove the user message so it doesn't get duplicated on retry.
            session.undo()

        # Display token usage.
        token_count = session.get_token_count()
        if token_count >= 0:
            budget = session.context_budget
            print_system(f"[tokens: ~{token_count} / {budget} budget]")


if __name__ == "__main__":
    main()
