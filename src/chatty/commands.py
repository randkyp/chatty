"""
Slash command parsing and dispatch.

All slash commands return a CommandResult: either a message to display,
a signal to quit, or None (handled internally).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console

from chatty.chat_session import ChatSession
from chatty.config import AppConfig, load_profile_by_name, save_profile
from chatty.images import get_clipboard_image, encode_image_file, encode_image

console = Console()


@dataclass
class CommandResult:
    """Result of executing a slash command."""

    quit: bool = False
    message: str | None = None


# ── Sampler helpers ────────────────────────────────────────────────────────

def _parse_value(raw: str) -> Any:
    """Parse a string into a native Python type (bool, int, float, or str)."""
    lower = raw.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in ("null", "none"):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _set_nested(d: dict[str, Any], dotpath: str, value: Any) -> None:
    """Set a value in a nested dict using dot notation."""
    keys = dotpath.split(".")
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value


def _del_nested(d: dict[str, Any], dotpath: str) -> bool:
    """Delete a key from a nested dict using dot notation. Returns success."""
    keys = dotpath.split(".")
    for key in keys[:-1]:
        if key not in d or not isinstance(d[key], dict):
            return False
        d = d[key]
    if keys[-1] in d:
        del d[keys[-1]]
        return True
    return False


# ── Command dispatch ──────────────────────────────────────────────────────

def handle_command(
    raw_input: str,
    session: ChatSession,
    cfg: AppConfig,
) -> CommandResult:
    """Parse and execute a slash command. Returns a CommandResult."""
    # Split into command + argument.
    parts = raw_input.strip().split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    available_commands = [
        "/quit", "/exit", "/clear", "/undo", "/system",
        "/ctx", "/genmax", "/profile", "/samplers", "/save",
        "/image", "/list", "/help"
    ]

    matches = [c for c in available_commands if c.startswith(cmd)]
    if len(matches) == 1:
        cmd = matches[0]
    elif len(matches) > 1:
        if cmd not in matches:
            return CommandResult(message=f"Ambiguous command '{cmd}'. Matches: {', '.join(matches)}")

    match cmd:
        case "/quit" | "/exit":
            return CommandResult(quit=True)

        case "/clear":
            session.clear()
            return CommandResult(message="History cleared.")

        case "/undo":
            if session.undo():
                return CommandResult(message="Last exchange removed.")
            else:
                return CommandResult(message="Nothing to undo.")

        case "/system":
            return _cmd_system(arg, session, cfg)

        case "/ctx":
            return _cmd_ctx(arg, session)

        case "/genmax":
            return _cmd_genmax(arg, session, cfg)

        case "/profile":
            return _cmd_profile(arg, session, cfg)

        case "/samplers":
            return _cmd_samplers(arg, cfg)

        case "/image":
            return _cmd_image(arg, session)

        case "/save":
            return _cmd_save(cfg, session)

        case "/list":
            return _cmd_list(session)

        case "/help":
            return _cmd_help()

        case _:
            return CommandResult(message=f"Unknown command: {cmd}")


# ── Individual command handlers ───────────────────────────────────────────

def _cmd_system(arg: str, session: ChatSession, cfg: AppConfig) -> CommandResult:
    if not arg:
        if session.system_prompt:
            return CommandResult(message=f"System prompt: {session.system_prompt}")
        return CommandResult(message="No system prompt set.")
    if arg.lower() in ("disable", "none", "clear", "del", "rm", "off"):
        session.system_prompt = None
        cfg.profile.system_prompt = None
        return CommandResult(message="System prompt disabled.")
    session.system_prompt = arg
    cfg.profile.system_prompt = arg
    return CommandResult(message="System prompt updated.")


def _cmd_ctx(arg: str, session: ChatSession) -> CommandResult:
    if not arg:
        budget = session.ctx_size - session.genmax
        used = session.get_token_count()
        n_msgs = len(session.messages)
        pct = (used / budget * 100) if budget > 0 and used >= 0 else 0

        lines = [
            f"Context window : {session.ctx_size} tokens",
            f"GenMax reserve : {session.genmax}",
            f"Usable budget  : {budget}",
            f"Current usage  : ~{used} tokens ({pct:.0f}%)" if used >= 0 else "Current usage  : (unavailable)",
            f"Messages       : {n_msgs}",
        ]
        return CommandResult(message="\n".join(lines))
    try:
        val = int(arg)
        session.ctx_size = val
        return CommandResult(message=f"Context size set to {val}.")
    except ValueError:
        return CommandResult(message="Usage: /ctx <integer>")


def _cmd_genmax(arg: str, session: ChatSession, cfg: AppConfig) -> CommandResult:
    if not arg:
        return CommandResult(message=f"Max generation tokens: {session.genmax}")
    try:
        val = int(arg)
        session.genmax = val
        cfg.profile.genmax = val
        return CommandResult(message=f"Max generation tokens set to {val}.")
    except ValueError:
        return CommandResult(message="Usage: /genmax <integer>")


def _cmd_profile(arg: str, session: ChatSession, cfg: AppConfig) -> CommandResult:
    if not arg:
        return CommandResult(message=f"Active profile: {cfg.profile.name}")
    try:
        new_profile, raw = load_profile_by_name(cfg.config_path, arg)
        cfg.profile = new_profile
        cfg._raw = raw
        session.system_prompt = new_profile.system_prompt
        if new_profile.ctx_size is not None:
            session.ctx_size = new_profile.ctx_size
        if new_profile.genmax is not None:
            session.genmax = new_profile.genmax
        return CommandResult(message=f"Switched to profile '{arg}'.")
    except SystemExit:
        return CommandResult(message=f"Profile '{arg}' not found.")


def _cmd_samplers(arg: str, cfg: AppConfig) -> CommandResult:
    parts = arg.split(None, 1)
    sub = parts[0].lower() if parts else ""
    sub_arg = parts[1] if len(parts) > 1 else ""

    if not sub or sub == "show":
        if cfg.profile.samplers:
            formatted = json.dumps(cfg.profile.samplers, indent=2, ensure_ascii=False)
            return CommandResult(message=f"Active samplers:\n{formatted}")
        return CommandResult(message="No samplers active.")

    if sub == "disable":
        cfg.profile.samplers.clear()
        return CommandResult(message="All samplers cleared.")

    if sub == "rm":
        if not sub_arg:
            return CommandResult(message="Usage: /samplers rm <key>")
        if _del_nested(cfg.profile.samplers, sub_arg):
            return CommandResult(message=f"Removed sampler '{sub_arg}'.")
        return CommandResult(message=f"Sampler '{sub_arg}' not found.")

    # /samplers <key> <value> — set a sampler.
    key = sub
    if not sub_arg:
        return CommandResult(message="Usage: /samplers <key> <value>")
    value = _parse_value(sub_arg)
    _set_nested(cfg.profile.samplers, key, value)
    return CommandResult(message=f"Set sampler '{key}' = {value!r}.")


def _cmd_save(cfg: AppConfig, session: ChatSession) -> CommandResult:
    # Sync runtime state back to profile before saving.
    cfg.profile.system_prompt = session.system_prompt
    cfg.profile.ctx_size = session.ctx_size
    cfg.profile.genmax = session.genmax
    try:
        save_profile(cfg)
        return CommandResult(message=f"Profile '{cfg.profile.name}' saved to {cfg.config_path}.")
    except Exception as e:
        return CommandResult(message=f"Failed to save: {e}")


def _cmd_image(arg: str, session: ChatSession) -> CommandResult:
    arg = arg.strip()
    if not arg or arg.lower() == "clipboard":
        return _paste_from_clipboard(session)

    try:
        # Check if the path is surrounded by quotes
        if (arg.startswith('"') and arg.endswith('"')) or (arg.startswith("'") and arg.endswith("'")):
            arg = arg[1:-1]
        arg = re.sub(r'\\(.)', r'\1', arg)
        path = Path(arg).expanduser().resolve()
        if not path.exists():
            return CommandResult(message=f"File not found: {arg}")
        if not path.is_file():
            return CommandResult(message=f"Path is not a file: {arg}")

        res = encode_image_file(path)
        if not res:
            return CommandResult(message=f"Failed to load or encode image: {arg}")

        data_url, mime = res
        session.pending_images.append({
            "data_url": data_url,
            "path": str(path),
            "mime_type": mime,
        })
        return CommandResult(message=f"Image attached: {path}")
    except Exception as e:
        return CommandResult(message=f"Error loading image: {e}")


def _paste_from_clipboard(session: ChatSession) -> CommandResult:
    try:
        img_bytes = get_clipboard_image()
        if not img_bytes:
            return CommandResult(message="No image found in clipboard or clipboard tools are missing.")

        # Convert raw clipboard image to base64
        data_url = encode_image(img_bytes, "image/png")

        session.pending_images.append({
            "data_url": data_url,
            "path": None,
            "mime_type": "image/png",
        })
        return CommandResult(message="Image from clipboard attached.")
    except Exception as e:
        return CommandResult(message=f"Error pasting image: {e}")


def _cmd_list(session: ChatSession) -> CommandResult:
    import shutil
    from rich.text import Text

    payload_messages = session.build_payload_messages()
    if payload_messages is None:
        console.print("[warning]⚠ Could not build context payload (budget exceeded).[/]")
        return CommandResult()
    if not payload_messages:
        console.print("[system_msg]No messages in current context window.[/]")
        return CommandResult()

    cols, _ = shutil.get_terminal_size((80, 24))
    margin = 4
    available_width = max(cols - margin, 20)

    total_msgs = len(payload_messages)
    index_width = len(str(total_msgs))

    for idx, msg in enumerate(payload_messages, 1):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # Extract text from content
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text_parts = []
            for part in content:
                if part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif part.get("type") == "image_url":
                    text_parts.append("[Image]")
            text = "".join(text_parts)

        # Get first line, stripped of leading/trailing whitespace
        first_line = text.splitlines()[0].strip() if text else ""

        # Format prefix and select style
        if role == "system":
            prefix = "⚙️ "
            style = "system_msg"
        elif role == "user":
            prefix = "👤 "
            style = "user"
        elif role == "assistant":
            prefix = "🤖 "
            style = "assistant"
        else:
            prefix = f"{role.upper()}: "
            style = "system_msg"

        # Format index prefix right-aligned (e.g. " 1. ") using "1." notation
        num_str = f"{idx}."
        idx_prefix = num_str.rjust(index_width + 1) + " "
        idx_len = len(idx_prefix)
        prefix_len = len(prefix)

        max_content_len = available_width - (idx_len + prefix_len)

        if len(first_line) > max_content_len:
            # We want total length of content_show to be max_content_len.
            # Unicode ellipsis "…" has length 1.
            trunc_len = max_content_len - 1
            if trunc_len > 0:
                content_show = first_line[:trunc_len] + "…"
            else:
                content_show = "…"
        else:
            content_show = first_line

        line_text = Text()
        line_text.append(idx_prefix, style="dim")
        line_text.append(prefix, style=style)
        line_text.append(content_show)
        console.print(line_text)

    return CommandResult()


def _cmd_help() -> CommandResult:
    from rich.text import Text

    help_lines = [
        ("/help", "Show this help summary."),
        ("/quit, /exit", "Exit the application."),
        ("/clear", "Clear active chat history (excluding system prompt)."),
        ("/undo", "Remove the last user/assistant exchange."),
        ("/list", "Preview first line of active context window messages."),
        ("/system [prompt]", "Show, set, or clear the system prompt."),
        ("/ctx [size]", "Show context window details or set context size."),
        ("/genmax [tokens]", "Show or set max generation tokens."),
        ("/profile [name]", "Show active profile or switch connection profile."),
        ("/samplers [opts]", "Show, set, or remove generation samplers."),
        ("/image [file]", "Attach an image from file path or clipboard."),
        ("/save", "Save active profile settings to config.toml."),
    ]

    console.print("[system_msg]Available slash commands:[/]")
    for cmd, desc in help_lines:
        line = Text()
        line.append(f"  {cmd:<18}", style="user")
        line.append(" - ")
        line.append(desc)
        console.print(line)

    return CommandResult()
