"""
Slash command parsing and dispatch.

All slash commands return a CommandResult: either a message to display,
a signal to quit, or None (handled internally).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from rich.console import Console

from chatty.chat_session import ChatSession
from chatty.config import AppConfig, Profile, load_profile_by_name, save_profile

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

        case "/save":
            return _cmd_save(cfg, session)

        case _:
            return CommandResult(message=f"Unknown command: {cmd}")


# ── Individual command handlers ───────────────────────────────────────────

def _cmd_system(arg: str, session: ChatSession, cfg: AppConfig) -> CommandResult:
    if not arg:
        if session.system_prompt:
            return CommandResult(message=f"System prompt: {session.system_prompt}")
        return CommandResult(message="No system prompt set.")
    if arg.lower() in ("disable", "none"):
        session.system_prompt = None
        cfg.profile.system_prompt = None
        return CommandResult(message="System prompt disabled.")
    session.system_prompt = arg
    cfg.profile.system_prompt = arg
    return CommandResult(message="System prompt updated.")


def _cmd_ctx(arg: str, session: ChatSession) -> CommandResult:
    if not arg:
        return CommandResult(message=f"Context size: {session.ctx_size}")
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
