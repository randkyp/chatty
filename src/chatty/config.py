"""
Configuration management: TOML loading, profile resolution, CLI argument parsing.

Profiles live under [profile.<name>] in config.toml.  Every field except
`base_url` is optional.  The `samplers` sub-table is an arbitrary dict that
gets passed through recursively into the API payload.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit

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
    enter_sends: bool = False
    raw_output: bool = False
    # Stores the raw parsed TOML so we can write back on /save.
    _raw: dict[str, Any] = field(default_factory=dict, repr=False)


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
        available = ", ".join(profiles.keys()) or "(none)"
        print(f"[error] Profile '{name}' not found. Available: {available}")
        sys.exit(1)
    data = profiles[name]
    if "base_url" not in data:
        print(f"[error] Profile '{name}' is missing required field 'base_url'.")
        sys.exit(1)
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
        "--enter-sends",
        action="store_true",
        help="Enter sends message (Shift+Enter for newlines).",
    )
    parser.add_argument(
        "-r",
        "--raw",
        action="store_true",
        help="Raw streaming output (no Markdown rendering).",
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
        enter_sends=args.enter_sends,
        raw_output=args.raw,
        _raw=raw,
    )


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

    if p.samplers:
        if "samplers" not in p_table:
            p_table["samplers"] = tomlkit.table()
        s_table = p_table["samplers"]
        for k, v in p.samplers.items():
            s_table[k] = v
        for k in list(s_table.keys()):
            if k not in p.samplers:
                del s_table[k]
    elif "samplers" in p_table:
        del p_table["samplers"]

    cfg.config_path.write_text(tomlkit.dumps(doc))
