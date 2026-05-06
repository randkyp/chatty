"""
Terminal UI: prompt_toolkit input, rich markdown rendering.

Uses native terminal scrolling (no curses alternate buffer).
Input is multiline by default; send with Meta+Enter (Esc then Enter) or
Ctrl+Enter (where the terminal supports it).
"""

from __future__ import annotations

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

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

def _build_key_bindings() -> KeyBindings:
    """Create key bindings: Enter inserts newline, Meta+Enter / Ctrl+Enter submits."""
    kb = KeyBindings()

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


def create_prompt_session() -> PromptSession:
    """Build a PromptSession with multiline input and custom key bindings."""
    return PromptSession(
        key_bindings=_build_key_bindings(),
        multiline=True,
        prompt_continuation=lambda width, line_number, wrap_count: "… ",
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

def print_assistant_chunk(text: str) -> None:
    """Print a raw streaming chunk (no newline, immediate flush)."""
    console.print(text, end="", highlight=False)


def print_assistant_done() -> None:
    """Print a newline after the streaming is finished."""
    console.print()


def render_assistant_message(text: str) -> None:
    """Render a complete assistant message with markdown formatting."""
    console.print()
    md = Markdown(text)
    console.print(Panel(md, title="[assistant]Assistant[/]", border_style="green", padding=(0, 1)))
    console.print()


def render_user_message(text: str) -> None:
    """Render the user's message for visual confirmation."""
    console.print()
    console.print(Panel(Text(text), title="[user]You[/]", border_style="cyan", padding=(0, 1)))


def print_system(msg: str) -> None:
    """Print a system/informational message."""
    console.print(f"[system_msg]{msg}[/]")


def print_warning(msg: str) -> None:
    """Print a warning."""
    console.print(f"[warning]⚠ {msg}[/]")


def print_error(msg: str) -> None:
    """Print an error."""
    console.print(f"[error]✗ {msg}[/]")


def print_welcome(profile_name: str, model: str, ctx: int, genmax: int) -> None:
    """Print a welcome banner."""
    console.print()
    console.print(
        Panel(
            f"[bold]Profile:[/] {profile_name}\n"
            f"[bold]Model:[/]   {model}\n"
            f"[bold]Context:[/] {ctx}  [bold]GenMax:[/] {genmax}\n\n"
            "[dim]Multiline input. Submit with Meta+Enter (Esc→Enter) or Ctrl+Enter.\n"
            "Type /quit to exit. /clear, /undo, /system, /samplers, /save for more.[/]",
            title="[bold magenta]✦ Chatty[/]",
            border_style="magenta",
            padding=(0, 1),
        )
    )
    console.print()
