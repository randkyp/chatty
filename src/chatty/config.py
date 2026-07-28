"""
Configuration management: TOML loading, profile resolution, CLI argument parsing.

Profiles live under [profile.<name>] in config.toml.  Every field except
`base_url` is optional.  The `samplers` sub-table is an arbitrary dict that
gets passed through recursively into the API payload.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit

from chatty.api import fetch_model_metadata


class ProfileNotFoundError(Exception):
    """Raised when a requested profile is missing or malformed in the config."""

    def __init__(self, message: str, available: list[str] | None = None) -> None:
        super().__init__(message)
        self.available = available or []


# ── Default config template ────────────────────────────────────────────────

DEFAULT_CONFIG = """\
# Chatty – default configuration
# Each [profile.<name>] section defines a connection profile.
# Run with: chatty -p <name>

[profile.default]
base_url = "http://localhost:8080"
# api_key = ""
# model = ""
# system_prompt = "You are a helpful assistant."
# ctx_size = 8192
# genmax = 0

# [profile.default.samplers]
# temperature = 0.7
# top_p = 0.9
"""


# ── Data structures ────────────────────────────────────────────────────────


@dataclass
class Profile:
    """Runtime representation of a single connection profile."""

    name: str
    base_url: str
    api_key: str | None = None
    model: str | None = None
    system_prompt: str | None = None
    ctx_size: int | None = None
    genmax: int | None = None
    samplers: dict[str, Any] = field(default_factory=dict)


@dataclass
class AppConfig:
    """Top-level configuration resolved from TOML + CLI overrides."""

    config_path: Path
    profile: Profile
    debug: bool = False
    enter_sends: bool = True
    raw_output: bool = False
    autosave: bool = False
    # Stores the raw parsed TOML so we can write back on /save.
    _raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def switch_profile(self, name: str) -> None:
        """Load *name* from this config's file and make it the active profile.

        Raises ProfileNotFoundError if the profile is missing or malformed.
        """
        profile, raw = load_profile_by_name(self.config_path, name)
        self.profile = profile
        self._raw = raw


# ── Helpers ────────────────────────────────────────────────────────────────


def _ensure_config(path: Path) -> Path:
    """Create a default config file if it doesn't exist. Returns the path."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG)
    return path


def _load_profile(raw: dict[str, Any], name: str) -> Profile:
    """Extract a Profile from parsed TOML data."""
    profiles = raw.get("profile", {})
    if name not in profiles:
        available = list(profiles.keys())
        avail_str = ", ".join(available) or "(none)"
        raise ProfileNotFoundError(
            f"Profile '{name}' not found. Available: {avail_str}",
            available=available,
        )
    data = profiles[name]
    if "base_url" not in data:
        raise ProfileNotFoundError(f"Profile '{name}' is missing required field 'base_url'.")
    return Profile(
        name=name,
        base_url=data["base_url"].rstrip("/"),
        api_key=data.get("api_key"),
        model=data.get("model"),
        system_prompt=data.get("system_prompt"),
        ctx_size=data.get("ctx_size"),
        genmax=data.get("genmax"),
        samplers=dict(data.get("samplers", {})),
    )


def load_profile_by_name(config_path: Path, name: str) -> tuple[Profile, dict[str, Any]]:
    """Load a profile from a config file by name. Returns (Profile, raw_toml)."""
    raw = tomlkit.loads(config_path.read_text())
    return _load_profile(raw, name), raw


# ── CLI argument parsing ───────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="chatty",
        description="Interactive CLI chat with any OpenAI-compatible endpoint.",
    )
    parser.add_argument(
        "-p",
        "--profile",
        default="default",
        help="Profile name from config.toml (default: 'default').",
    )
    parser.add_argument(
        "-c",
        "--config",
        default="config.toml",
        help="Path to config.toml (default: ./config.toml).",
    )
    parser.add_argument(
        "-m",
        "--model",
        default=None,
        help="Override model name for this session.",
    )
    parser.add_argument(
        "-s",
        "--system",
        default=None,
        help="Override system prompt for this session.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print the JSON payload before each API call.",
    )
    parser.add_argument(
        "-e",
        "--multiline",
        action="store_true",
        help="Enter inserts a newline; submit with Meta+Enter or Ctrl+Enter.",
    )
    parser.add_argument(
        "-r",
        "--raw",
        action="store_true",
        help="Raw streaming output (no Markdown rendering).",
    )
    parser.add_argument(
        "--autosave",
        action="store_true",
        help="Autosave the session to ~/.config/chatty/session.json on exit.",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Launch the web UI server instead of the CLI.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for the web server (with --web; default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for the web server (with --web; default: 8000).",
    )
    return parser.parse_args(argv)


# ── Top-level loader ──────────────────────────────────────────────────────


def load_config(argv: list[str] | None = None) -> AppConfig:
    """Parse CLI args, read TOML, apply overrides, return AppConfig."""
    args = parse_args(argv)
    config_arg = Path(args.config)
    if config_arg.is_absolute():
        config_path = _ensure_config(config_arg)
    else:
        home_config_dir = Path.home() / ".config" / "chatty"
        home_config_path = home_config_dir / config_arg
        local_config_path = config_arg

        if home_config_path.exists():
            config_path = home_config_path
        elif local_config_path.exists():
            config_path = local_config_path
        else:
            config_path = _ensure_config(home_config_path)

    raw = tomlkit.loads(config_path.read_text())
    profile = _load_profile(raw, args.profile)

    # CLI overrides take priority over TOML values.
    if args.model:
        profile.model = args.model
    if args.system:
        profile.system_prompt = args.system

    return AppConfig(
        config_path=config_path,
        profile=profile,
        debug=args.debug,
        enter_sends=not args.multiline,
        raw_output=args.raw,
        autosave=args.autosave,
        _raw=raw,
    )


def resolve_limits(cfg: AppConfig) -> None:
    """Fill in ctx_size / genmax / model from server metadata if not set in profile."""
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


# ── Save back to TOML ────────────────────────────────────────────────────


def save_profile(cfg: AppConfig) -> None:
    """Write the active profile back to config.toml, preserving other profiles."""
    doc = tomlkit.loads(cfg.config_path.read_text())
    p = cfg.profile

    if "profile" not in doc:
        doc["profile"] = tomlkit.table()

    profiles = doc["profile"]
    if p.name not in profiles:
        profiles[p.name] = tomlkit.table()

    p_table = profiles[p.name]
    p_table["base_url"] = p.base_url

    if p.api_key is not None:
        p_table["api_key"] = p.api_key
    elif "api_key" in p_table:
        del p_table["api_key"]

    if p.model is not None:
        p_table["model"] = p.model
    elif "model" in p_table:
        del p_table["model"]

    if p.system_prompt is not None:
        p_table["system_prompt"] = p.system_prompt
    elif "system_prompt" in p_table:
        del p_table["system_prompt"]

    if p.ctx_size is not None:
        p_table["ctx_size"] = p.ctx_size
    elif "ctx_size" in p_table:
        del p_table["ctx_size"]

    if p.genmax is not None:
        p_table["genmax"] = p.genmax
    elif "genmax" in p_table:
        del p_table["genmax"]

    # TOML has no null type, so drop None-valued samplers rather than emitting
    # invalid TOML (the old _serialise_value repr() fallback could do exactly that).
    writable_samplers = {k: v for k, v in p.samplers.items() if v is not None}
    if writable_samplers:
        if "samplers" not in p_table:
            p_table["samplers"] = tomlkit.table()
        s_table = p_table["samplers"]
        for k, v in writable_samplers.items():
            s_table[k] = v
        for k in list(s_table.keys()):
            if k not in writable_samplers:
                del s_table[k]
    elif "samplers" in p_table:
        del p_table["samplers"]

    cfg.config_path.write_text(tomlkit.dumps(doc))
