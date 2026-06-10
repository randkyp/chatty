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
from chatty.images import encode_image, encode_image_file, get_clipboard_image

console = Console()


@dataclass
class CommandResult:
    """Result of executing a slash command."""

    quit: bool = False
    message: str | None = None
    ephemeral_prompt: str | None = None
    copy_last: bool = False


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


def _sanitize_path_arg(arg: str) -> tuple[Path, bool]:
    """
    Sanitize the filename portion of a path argument and add an extension if missing.
    Returns (sanitized_path, extension_added).
    """
    path = Path(arg)
    name = path.name

    # Replace non-alphanumeric/hyphen/dot characters with underscore
    sanitized = re.sub(r"[^\w\-\.]", "_", name)
    # Collapse multiple underscores
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")

    if not sanitized:
        sanitized = "session"

    added_ext = False
    if not Path(sanitized).suffix:
        sanitized += ".json"
        added_ext = True

    return path.with_name(sanitized), added_ext


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
        "/quit",
        "/exit",
        "/clear",
        "/undo",
        "/system",
        "/ctx",
        "/genmax",
        "/profile",
        "/samplers",
        "/sampelrs",
        "/save",
        "/load",
        "/image",
        "/list",
        "/help",
        "/models",
        "/btw",
        "/copy",
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
            return _cmd_samplers(arg, cfg, session)

        case "/image":
            return _cmd_image(arg, session)

        case "/save":
            return _cmd_save_session(arg, session, cfg)

        case "/load":
            return _cmd_load_session(arg, session, cfg)

        case "/list":
            return _cmd_list(session)

        case "/help":
            return _cmd_help()

        case "/models":
            return _cmd_models(arg, cfg)

        case "/btw":
            return _cmd_btw(arg)

        case "/copy":
            return CommandResult(copy_last=True)

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
        budget = session.context_budget
        used = session.get_token_count()
        padding = session.get_padding_count()
        n_msgs = len(session.messages)
        total_used = used + padding if used >= 0 else -1
        pct = (total_used / budget * 100) if budget > 0 and total_used >= 0 else 0

        lines = [
            f"Context window : {session.ctx_size} tokens",
            f"GenMax reserve : {'unlimited' if session.genmax == 0 else session.genmax}",
            f"Usable budget  : {budget}",
            f"Current usage  : ~{used} tokens + {padding} pad ({pct:.0f}%)"
            if used >= 0
            else "Current usage  : (unavailable)",
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
        val = "unlimited" if session.genmax == 0 else session.genmax
        return CommandResult(message=f"Max generation tokens: {val}")
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


def _cmd_samplers(arg: str, cfg: AppConfig, session: ChatSession) -> CommandResult:
    parts = arg.split(None, 1)
    sub = parts[0].lower() if parts else ""
    sub_arg = parts[1] if len(parts) > 1 else ""

    if sub == "save":
        # Sync runtime state back to profile before saving.
        cfg.profile.system_prompt = session.system_prompt
        cfg.profile.ctx_size = session.ctx_size
        cfg.profile.genmax = session.genmax
        try:
            save_profile(cfg)
            return CommandResult(message=f"Profile '{cfg.profile.name}' saved to {cfg.config_path}.")
        except Exception as e:
            return CommandResult(message=f"Failed to save: {e}")

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


def _cmd_save_session(arg: str, session: ChatSession, cfg: AppConfig) -> CommandResult:
    home_config_dir = Path.home() / ".config" / "chatty"
    arg = arg.strip()

    if arg:
        path, _ = _sanitize_path_arg(arg)
        if path.is_absolute():
            resolved_path = path
        elif arg.startswith("./") or arg.startswith("../") or arg.startswith(".\\") or arg.startswith("..\\"):
            resolved_path = path.resolve()
        else:
            resolved_path = home_config_dir / path
    else:
        jsonl_path = home_config_dir / "session.jsonl"
        json_path = home_config_dir / "session.json"
        if jsonl_path.exists() and not json_path.exists():
            resolved_path = jsonl_path
        else:
            resolved_path = json_path

    use_jsonl = resolved_path.suffix == ".jsonl"

    try:
        resolved_path.parent.mkdir(parents=True, exist_ok=True)

        if use_jsonl:
            with open(resolved_path, "w", encoding="utf-8") as f:
                metadata = {
                    "system_prompt": session.system_prompt,
                    "ctx_size": session.ctx_size,
                    "genmax": session.genmax,
                }
                f.write(json.dumps(metadata, ensure_ascii=False) + "\n")
                for msg in session.messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        else:
            data = {
                "system_prompt": session.system_prompt,
                "ctx_size": session.ctx_size,
                "genmax": session.genmax,
                "messages": session.messages,
            }
            with open(resolved_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

        return CommandResult(message=f"Session saved to {resolved_path}.")
    except Exception as e:
        return CommandResult(message=f"Failed to save session: {e}")


def _cmd_load_session(arg: str, session: ChatSession, cfg: AppConfig) -> CommandResult:
    home_config_dir = Path.home() / ".config" / "chatty"
    arg = arg.strip()

    if arg:
        path, added_ext = _sanitize_path_arg(arg)

        def resolve_path(p: Path) -> Path:
            if p.is_absolute():
                return p
            elif arg.startswith("./") or arg.startswith("../") or arg.startswith(".\\") or arg.startswith("..\\"):
                return p.resolve()
            else:
                home_session_path = home_config_dir / p
                local_session_path = p
                config_session_path = cfg.config_path.parent / p

                if home_session_path.exists():
                    return home_session_path
                elif local_session_path.exists():
                    return local_session_path
                elif config_session_path.exists():
                    return config_session_path
                else:
                    return home_session_path

        resolved_path = resolve_path(path)

        if not resolved_path.exists() and added_ext:
            alt_path = path.with_suffix(".jsonl")
            alt_resolved = resolve_path(alt_path)
            if alt_resolved.exists():
                resolved_path = alt_resolved

        if not resolved_path.exists():
            return CommandResult(message=f"Session file not found: {resolved_path}")
    else:
        paths_to_check = [
            home_config_dir / "session.json",
            home_config_dir / "session.jsonl",
            Path("session.json"),
            Path("session.jsonl"),
            cfg.config_path.parent / "session.json",
            cfg.config_path.parent / "session.jsonl",
        ]

        resolved_path = None
        for p in paths_to_check:
            if p.exists():
                resolved_path = p
                break

        if not resolved_path:
            return CommandResult(message=f"No saved session found in {home_config_dir} or current directory.")

    try:
        messages = []
        system_prompt = None
        ctx_size = None
        genmax = None

        if resolved_path.suffix == ".jsonl":
            with open(resolved_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if "role" in data and "content" in data:
                        messages.append(data)
                    else:
                        if "system_prompt" in data:
                            system_prompt = data["system_prompt"]
                        if "ctx_size" in data:
                            ctx_size = data["ctx_size"]
                        if "genmax" in data:
                            genmax = data["genmax"]
        else:
            with open(resolved_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                system_prompt = data.get("system_prompt")
                ctx_size = data.get("ctx_size")
                genmax = data.get("genmax")
                messages = data.get("messages", [])
            elif isinstance(data, list):
                messages = data

        session.messages = messages
        if system_prompt is not None:
            session.system_prompt = system_prompt
        if ctx_size is not None:
            session.ctx_size = ctx_size
        if genmax is not None:
            session.genmax = genmax

        session.pending_images.clear()

        if system_prompt is not None:
            cfg.profile.system_prompt = system_prompt
        if ctx_size is not None:
            cfg.profile.ctx_size = ctx_size
        if genmax is not None:
            cfg.profile.genmax = genmax

        return CommandResult(message=f"Session restored from {resolved_path} ({len(messages)} messages loaded).")
    except Exception as e:
        return CommandResult(message=f"Failed to load session: {e}")


def _cmd_image(arg: str, session: ChatSession) -> CommandResult:
    arg = arg.strip()
    if not arg or arg.lower() == "clipboard":
        return _paste_from_clipboard(session)

    try:
        # Check if the path is surrounded by quotes
        if (arg.startswith('"') and arg.endswith('"')) or (arg.startswith("'") and arg.endswith("'")):
            arg = arg[1:-1]
        arg = re.sub(r"\\(.)", r"\1", arg)
        path = Path(arg).expanduser().resolve()
        if not path.exists():
            return CommandResult(message=f"File not found: {arg}")
        if not path.is_file():
            return CommandResult(message=f"Path is not a file: {arg}")

        res = encode_image_file(path)
        if not res:
            return CommandResult(message=f"Failed to load or encode image: {arg}")

        data_url, mime = res
        session.pending_images.append(
            {
                "data_url": data_url,
                "path": str(path),
                "mime_type": mime,
            }
        )
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

        session.pending_images.append(
            {
                "data_url": data_url,
                "path": None,
                "mime_type": "image/png",
            }
        )
        return CommandResult(message="Image from clipboard attached.")
    except Exception as e:
        return CommandResult(message=f"Error pasting image: {e}")


def _cmd_list(session: ChatSession) -> CommandResult:
    payload_messages = session.build_payload_messages()
    if payload_messages is None:
        return CommandResult(message="⚠ Could not build context payload (budget exceeded).")
    if not payload_messages:
        return CommandResult(message="No messages in current context window.")

    available_width = 76
    total_msgs = len(payload_messages)
    index_width = len(str(total_msgs))

    out = []
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

        # Format prefix
        if role == "system":
            prefix = "[SYS] "
        elif role == "user":
            prefix = "[YOU] "
        elif role == "assistant":
            prefix = "[BOT] "
        else:
            prefix = f"{role.upper()}: "

        num_str = f"{idx}."
        idx_prefix = num_str.rjust(index_width + 1) + " "
        idx_len = len(idx_prefix)
        prefix_len = len(prefix)

        max_content_len = available_width - (idx_len + prefix_len)

        if len(first_line) > max_content_len:
            trunc_len = max_content_len - 1
            if trunc_len > 0:
                content_show = first_line[:trunc_len] + "…"
            else:
                content_show = "…"
        else:
            content_show = first_line

        out.append(f"{idx_prefix}{prefix}{content_show}")

    return CommandResult(message="\n".join(out))


def _cmd_models(arg: str, cfg: AppConfig) -> CommandResult:
    arg = arg.strip()
    if not arg:
        from chatty.api import list_models

        models = list_models(cfg.profile.base_url, cfg.profile.api_key)
        if not models:
            return CommandResult(message="Failed to fetch models or no models found.")
        return CommandResult(message="Available models:\n" + "\n".join(f"- {m}" for m in models))
    else:
        cfg.profile.model = arg
        return CommandResult(message=f"Switched to model '{arg}'.")


def _cmd_btw(arg: str) -> CommandResult:
    if not arg:
        return CommandResult(message="Usage: /btw <message>")
    return CommandResult(ephemeral_prompt=arg)


def _cmd_help() -> CommandResult:
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
        ("/samplers [opts]", "Show, set, remove samplers, or save settings (/samplers save)."),
        ("/image [file]", "Attach an image from file path or clipboard."),
        ("/save [file]", "Save active chat session to session.json or custom file."),
        ("/load [file]", "Load chat session from session.json or custom file."),
        ("/models [name]", "List available models or switch to a specific model."),
        ("/btw [msg]", "Send an ephemeral message without adding it to the context window."),
        ("/copy", "Copy the last assistant response to the clipboard."),
    ]

    out = ["Available slash commands:"]
    for cmd, desc in help_lines:
        out.append(f"  {cmd:<18} - {desc}")

    return CommandResult(message="\n".join(out))
