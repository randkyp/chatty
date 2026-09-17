"""
Terminal UI: prompt_toolkit input, rich markdown rendering.

Uses native terminal scrolling (no curses alternate buffer).

Default mode: Enter sends immediately; Shift+Enter (Esc→Enter)
inserts a newline.

With --multiline / -e: multiline input; send with Meta+Enter
(Esc then Enter) or Ctrl+Enter (where the terminal supports it).
"""

from __future__ import annotations

import re
import shutil
from typing import TYPE_CHECKING

import tomlkit
from prompt_toolkit import PromptSession, prompt
from prompt_toolkit.completion import Completer, Completion, PathCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from rich.console import Console
from rich.theme import Theme

if TYPE_CHECKING:
    from collections.abc import Iterable

    from prompt_toolkit.input.base import Input
    from prompt_toolkit.output.base import Output

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


def _build_key_bindings(enter_sends: bool = True) -> KeyBindings:
    """Create key bindings for the input prompt.

    When *enter_sends* is True (default):
        Enter submits; Shift+Enter (Escape→Enter) inserts a newline.
    When *enter_sends* is False:
        Enter inserts a newline; Meta+Enter / Ctrl+Enter submits.
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

    def _profile_names(self) -> list[str]:
        try:
            raw = tomlkit.loads(self.cfg.config_path.read_text())
            return list(raw.get("profile", {}).keys())
        except (OSError, tomlkit.exceptions.TOMLKitError):
            return []

    def _model_names(self) -> list[str]:
        from chatty.api import list_models

        if self.cfg.ephemeral_api_key_missing:
            return []
        return list_models(self.cfg.profile.base_url, self.cfg.effective_api_key)

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


def create_prompt_session(enter_sends: bool = True, completer: Completer | None = None) -> PromptSession:
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


def get_api_key_input() -> str | None:
    """Read an API key without echoing it; return None when cancelled."""
    try:
        return prompt("API key (input hidden) › ", is_password=True)
    except (EOFError, KeyboardInterrupt):
        return None


# ── Output rendering ──────────────────────────────────────────────────────


def print_user(msg: str) -> None:
    """Print the user's message cleanly to the scrollback buffer."""
    from rich.text import Text

    t = Text("You › ", style="user")
    t.append(msg)
    console.print(t)


def print_assistant_chunk(text: str) -> None:
    """Print a raw streaming chunk (no newline, immediate flush)."""
    console.print(text, end="", highlight=False, markup=False)


def print_assistant_done() -> None:
    """Print a newline after the streaming is finished."""
    console.print()


def print_system(msg: str) -> None:
    """Print a system/informational message."""
    console.print(msg, style="system_msg", markup=False)


def print_warning(msg: str) -> None:
    """Print a warning."""
    console.print(f"⚠ {msg}", style="warning", markup=False)


def print_error(msg: str) -> None:
    """Print an error."""
    console.print(f"✗ {msg}", style="error", markup=False)


def print_welcome(
    profile_name: str,
    model: str,
    ctx: int,
    genmax: int,
    enter_sends: bool = True,
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


# ── Model picker (terminal) ──────────────────────────────────────────────

MAX_PICKER_ROWS = 8


def model_picker_max_visible(term_height: int | None = None) -> int:
    """Max picker rows: at most eight, fewer when the terminal is short."""
    if term_height is None:
        try:
            term_height = shutil.get_terminal_size((80, 24)).lines
        except OSError:
            term_height = 24
    return max(1, min(MAX_PICKER_ROWS, term_height - 5))


def pick_model(
    models: list[str],
    current: str | None = None,
    *,
    _input: Input | None = None,
    _output: Output | None = None,
) -> str | None:
    """Interactive single-column picker over already-fetched *models*.

    Returns the selected model id, or None when cancelled. Renders inline
    (no alternate screen), erases itself afterward, and never refetches.
    Filtering is case-insensitive ordered-subsequence; only Up/Down moves
    the highlight; Enter selects; Escape/Ctrl+C cancels; Enter with no
    matches does nothing.
    """
    if not models:
        return None

    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension

    from chatty.commands import filter_model_choices

    max_visible = model_picker_max_visible()
    state: dict[str, object] = {
        "highlighted": current if current in models else models[0],
    }

    def _filtered() -> list[str]:
        return filter_model_choices(models, filter_buffer.text)

    def _highlight_index(filtered: list[str]) -> int:
        highlighted = state.get("highlighted")
        if highlighted in filtered:
            return filtered.index(highlighted)  # type: ignore[arg-type]
        return 0 if filtered else -1

    filter_buffer = Buffer(multiline=False)

    def _get_list_fragments():
        filtered = _filtered()
        # Keep highlight valid as the filter narrows.
        if filtered:
            if state.get("highlighted") not in filtered:
                state["highlighted"] = filtered[0]
        idx = _highlight_index(filtered)
        if not filtered:
            return [("class:picker-empty", "  No matching models")]
        # Sliding window so the highlight stays visible.
        total = len(filtered)
        count = min(max_visible, total)
        if idx < 0:
            idx = 0
        start = max(0, min(idx - count + 1, total - count))
        if idx < start:
            start = idx
        end = min(total, start + count)
        frags: list[tuple[str, str]] = []
        for i in range(start, end):
            name = filtered[i]
            marker = "> " if i == idx else "  "
            style = "class:picker-highlight" if i == idx else "class:picker-row"
            frags.append((style, f"{marker}{name}\n"))
        return frags

    list_control = FormattedTextControl(text=_get_list_fragments, focusable=False)
    list_window = Window(
        content=list_control,
        height=Dimension(max=max_visible, min=1),
        wrap_lines=False,
    )
    input_control = BufferControl(buffer=filter_buffer, focusable=True)
    input_window = Window(
        content=input_control,
        height=Dimension.exact(1),
        wrap_lines=False,
    )

    from prompt_toolkit.layout import VSplit

    root = HSplit(
        [
            VSplit([Window(content=FormattedTextControl(text="> "), width=Dimension.exact(2)), input_window]),
            list_window,
        ]
    )
    layout = Layout(container=root)
    layout.focus(input_window)

    kb = KeyBindings()

    @kb.add("up")
    def _(event):  # type: ignore[no-untyped-def]
        filtered = _filtered()
        if not filtered:
            return
        idx = _highlight_index(filtered)
        idx = (idx - 1) % len(filtered)
        state["highlighted"] = filtered[idx]

    @kb.add("down")
    def _(event):  # type: ignore[no-untyped-def]
        filtered = _filtered()
        if not filtered:
            return
        idx = _highlight_index(filtered)
        idx = (idx + 1) % len(filtered)
        state["highlighted"] = filtered[idx]

    @kb.add("enter")
    def _(event):  # type: ignore[no-untyped-def]
        filtered = _filtered()
        if not filtered:
            return
        idx = _highlight_index(filtered)
        event.app.exit(result=filtered[idx])

    @kb.add("escape")
    def _(event):  # type: ignore[no-untyped-def]
        event.app.exit(result=None)

    @kb.add("c-c")
    def _(event):  # type: ignore[no-untyped-def]
        event.app.exit(result=None)

    # Typing only filters; keep highlight stable and redraw.
    def _on_text_changed(_buffer) -> None:  # type: ignore[no-untyped-def]
        filtered = _filtered()
        if filtered:
            if state.get("highlighted") not in filtered:
                state["highlighted"] = filtered[0]
        try:
            app.invalidate()
        except NameError:
            pass

    try:
        filter_buffer.on_text_changed += _on_text_changed  # type: ignore[attr-defined]
    except AttributeError:
        pass

    app: Application[str | None] = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=False,
        erase_when_done=True,
        mouse_support=False,
        input=_input,
        output=_output,
    )
    try:
        return app.run()
    except (KeyboardInterrupt, EOFError):
        return None
