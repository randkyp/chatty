"""
Terminal UI: prompt_toolkit input, rich markdown rendering.

Uses native terminal scrolling (no curses alternate buffer).

Default mode: multiline input; send with Meta+Enter (Esc then Enter)
or Ctrl+Enter (where the terminal supports it).

With --enter-sends / -e: Enter sends immediately; Shift+Enter
(Esc→Enter) inserts a newline.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import tomlkit
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion, PathCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from rich.console import Console
from rich.theme import Theme

if TYPE_CHECKING:
    from collections.abc import Iterable

    from chatty.config import AppConfig

# ── Rich console setup ─────────────────────────────────────────────────────

_theme = Theme(
    {
        "user": "bold cyan",
        "assistant": "bold green",
        "system_msg": "dim italic",
        "warning": "bold yellow",
        "error": "bold red",
    }
)

console = Console(theme=_theme)


# ── prompt_toolkit input ───────────────────────────────────────────────────


def _build_key_bindings(enter_sends: bool = False) -> KeyBindings:
    """Create key bindings for the input prompt.

    When *enter_sends* is False (default):
        Enter inserts a newline; Meta+Enter / Ctrl+Enter submits.
    When *enter_sends* is True:
        Enter submits; Shift+Enter (Escape→Enter) inserts a newline.
    """
    kb = KeyBindings()

    if enter_sends:

        @kb.add(Keys.Enter)
        def _(event):  # type: ignore[no-untyped-def]
            """Plain Enter submits the buffer."""
            event.current_buffer.validate_and_handle()

        @kb.add(Keys.Escape, Keys.Enter)
        def _(event):  # type: ignore[no-untyped-def]
            """Shift+Enter (Esc→Enter) inserts a newline."""
            event.current_buffer.insert_text("\n")
    else:

        @kb.add(Keys.Enter)
        def _(event):  # type: ignore[no-untyped-def]
            """Plain Enter inserts a newline (multiline editing)."""
            event.current_buffer.insert_text("\n")

        @kb.add(Keys.Escape, Keys.Enter)
        def _(event):  # type: ignore[no-untyped-def]
            """Meta+Enter (Esc then Enter) submits the buffer."""
            event.current_buffer.validate_and_handle()

        @kb.add(Keys.ControlJ)  # Ctrl+Enter on many terminals
        def _(event):  # type: ignore[no-untyped-def]
            """Ctrl+Enter submits."""
            event.current_buffer.validate_and_handle()

    return kb


class ChattyCompleter(Completer):
    """Tab completion for slash commands, profile/model names, and @paths."""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._path_completer = PathCompleter(expanduser=True)
        self._model_cache: list[str] | None = None

    def _profile_names(self) -> list[str]:
        try:
            raw = tomlkit.loads(self.cfg.config_path.read_text())
            return list(raw.get("profile", {}).keys())
        except (OSError, tomlkit.exceptions.TOMLKitError):
            return []

    def _model_names(self) -> list[str]:
        if self._model_cache is None:
            from chatty.api import list_models

            self._model_cache = list_models(self.cfg.profile.base_url, self.cfg.profile.api_key)
        return self._model_cache

    def _word_completions(self, options: Iterable[str], word: str) -> Iterable[Completion]:
        for opt in options:
            if opt.startswith(word):
                yield Completion(opt, start_position=-len(word))

    def get_completions(self, document: Document, complete_event):  # type: ignore[no-untyped-def]
        text = document.text_before_cursor

        # @path completion: complete the fragment after the last unspaced '@'.
        at = text.rfind("@")
        if at != -1:
            fragment = text[at + 1 :]
            if not re.search(r"\s", fragment):
                sub_doc = Document(fragment, len(fragment))
                yield from self._path_completer.get_completions(sub_doc, complete_event)
                return

        # Command name (whole input is a single slash-word being typed).
        if re.fullmatch(r"/[a-zA-Z]*", text):
            from chatty.commands import COMMANDS

            yield from self._word_completions(COMMANDS, text)
            return

        # /profile <name>
        m = re.fullmatch(r"/profile\s+(\S*)", text)
        if m:
            yield from self._word_completions(self._profile_names(), m.group(1))
            return

        # /models <name>
        m = re.fullmatch(r"/models\s+(\S*)", text)
        if m:
            yield from self._word_completions(self._model_names(), m.group(1))
            return


def create_prompt_session(enter_sends: bool = False, completer: Completer | None = None) -> PromptSession:
    """Build a PromptSession with multiline input and custom key bindings."""
    return PromptSession(
        key_bindings=_build_key_bindings(enter_sends),
        multiline=True,
        completer=completer,
        complete_while_typing=False,  # Tab-triggered; avoids per-keystroke model fetches.
        prompt_continuation=lambda width, line_number, wrap_count: "… ",
        erase_when_done=True,
    )


def get_user_input(session: PromptSession) -> str | None:
    """Read user input. Returns None on Ctrl-D (EOF)."""
    try:
        return session.prompt(HTML("<b>You › </b>"))
    except EOFError:
        return None
    except KeyboardInterrupt:
        # Ctrl-C at the prompt: return empty to re-prompt.
        return ""


# ── Output rendering ──────────────────────────────────────────────────────


def print_user(msg: str) -> None:
    """Print the user's message cleanly to the scrollback buffer."""
    from rich.text import Text

    t = Text("You › ", style="user")
    t.append(msg)
    console.print(t)


def print_assistant_chunk(text: str) -> None:
    """Print a raw streaming chunk (no newline, immediate flush)."""
    console.print(text, end="", highlight=False)


def print_assistant_done() -> None:
    """Print a newline after the streaming is finished."""
    console.print()


def print_system(msg: str) -> None:
    """Print a system/informational message."""
    console.print(f"[system_msg]{msg}[/]")


def print_warning(msg: str) -> None:
    """Print a warning."""
    console.print(f"[warning]⚠ {msg}[/]")


def print_error(msg: str) -> None:
    """Print an error."""
    console.print(f"[error]✗ {msg}[/]")


def print_welcome(
    profile_name: str,
    model: str,
    ctx: int,
    genmax: int,
    enter_sends: bool = False,
) -> None:
    """Print a welcome banner."""
    console.print()
    console.print("[bold magenta]✦ Chatty[/]")
    console.print(f"[bold]Profile:[/] {profile_name}")
    console.print(f"[bold]Model:[/]   {model}")
    gen_display = "unlimited" if genmax == 0 else genmax
    console.print(f"[bold]Context:[/] {ctx}  [bold]GenMax:[/] {gen_display}")
    console.print()
    if enter_sends:
        console.print("[dim]Enter sends. Shift+Enter (Esc→Enter) for newlines.[/]")
    else:
        console.print("[dim]Multiline input. Submit with Meta+Enter (Esc→Enter) or Ctrl+Enter.[/]")
    console.print("[dim]Type /quit to exit. Type /help to show all available commands.[/]")
    console.print()
