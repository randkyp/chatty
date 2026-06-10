import sys

import pytest

from chatty.config import (
    DEFAULT_CONFIG,
    _ensure_config,
    _load_profile,
    _serialise_value,
    load_config,
    load_profile_by_name,
    parse_args,
    save_profile,
)


def test_ensure_config(tmp_path):
    config_file = tmp_path / "config.toml"
    assert not config_file.exists()

    returned_path = _ensure_config(config_file)
    assert returned_path == config_file
    assert config_file.exists()
    assert config_file.read_text() == DEFAULT_CONFIG


def test_load_profile_success():
    raw_toml = {
        "profile": {
            "default": {
                "base_url": "http://localhost:8000",
                "api_key": "testkey",
                "model": "testmodel",
                "system_prompt": "Hello",
                "ctx_size": 2048,
                "genmax": 512,
                "samplers": {
                    "temperature": 0.5,
                },
            }
        }
    }
    profile = _load_profile(raw_toml, "default")
    assert profile.name == "default"
    assert profile.base_url == "http://localhost:8000"
    assert profile.api_key == "testkey"
    assert profile.model == "testmodel"
    assert profile.system_prompt == "Hello"
    assert profile.ctx_size == 2048
    assert profile.genmax == 512
    assert profile.samplers == {"temperature": 0.5}


def test_load_profile_missing_base_url(monkeypatch):
    raw_toml = {"profile": {"default": {"api_key": "testkey"}}}
    # _load_profile calls sys.exit(1) on failure.
    exit_called = False

    def mock_exit(code):
        nonlocal exit_called
        exit_called = True
        raise SystemExit(code)

    monkeypatch.setattr(sys, "exit", mock_exit)

    with pytest.raises(SystemExit):
        _load_profile(raw_toml, "default")
    assert exit_called


def test_load_profile_missing_profile(monkeypatch):
    raw_toml = {"profile": {}}
    exit_called = False

    def mock_exit(code):
        nonlocal exit_called
        exit_called = True
        raise SystemExit(code)

    monkeypatch.setattr(sys, "exit", mock_exit)

    with pytest.raises(SystemExit):
        _load_profile(raw_toml, "nonexistent")
    assert exit_called


def test_load_profile_by_name(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("""
[profile.myprofile]
base_url = "http://localhost:8080"
api_key = "abc"
""")
    profile, raw = load_profile_by_name(config_file, "myprofile")
    assert profile.name == "myprofile"
    assert profile.base_url == "http://localhost:8080"
    assert profile.api_key == "abc"
    assert "profile" in raw


def test_parse_args():
    args = parse_args(["-p", "custom", "-c", "myconfig.toml", "-m", "gpt-4", "-s", "SysPrompt", "--debug", "-e", "-r"])
    assert args.profile == "custom"
    assert args.config == "myconfig.toml"
    assert args.model == "gpt-4"
    assert args.system == "SysPrompt"
    assert args.debug is True
    assert args.enter_sends is True
    assert args.raw is True


def test_load_config_absolute_path(tmp_path):
    config_file = tmp_path / "myconfig.toml"
    config_file.write_text("""
[profile.default]
base_url = "http://localhost:1234"
""")
    cfg = load_config(["-c", str(config_file.resolve())])
    assert cfg.config_path == config_file.resolve()
    assert cfg.profile.base_url == "http://localhost:1234"


def test_load_config_cli_overrides(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("""
[profile.default]
base_url = "http://localhost:1234"
model = "toml-model"
system_prompt = "toml-sys"
""")
    cfg = load_config(["-c", str(config_file.resolve()), "-m", "cli-model", "-s", "cli-sys"])
    assert cfg.profile.model == "cli-model"
    assert cfg.profile.system_prompt == "cli-sys"


def test_serialise_value():
    assert _serialise_value(True) == "true"
    assert _serialise_value(False) == "false"
    assert _serialise_value(123) == "123"
    assert _serialise_value(1.25) == "1.25"
    assert _serialise_value("hello") == '"hello"'
    assert _serialise_value("back\\slash") == '"back\\\\slash"'
    assert _serialise_value('qu"ote') == '"qu\\"ote"'
    assert _serialise_value({"a": 1, "b": "yes"}) == '{a = 1, b = "yes"}'
    assert _serialise_value([1, "two", False]) == '[1, "two", false]'
    assert _serialise_value(object) == repr(object)


def test_save_profile(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("""
[profile.default]
base_url = "http://localhost:8080"
api_key = "original-key"

[profile.other]
base_url = "http://localhost:9000"
""")

    cfg = load_config(["-c", str(config_file.resolve())])
    cfg.profile.api_key = "new-key"
    cfg.profile.samplers = {"temperature": 0.7, "nested": {"param": True}}

    save_profile(cfg)

    # Reload and check
    reloaded_cfg = load_config(["-c", str(config_file.resolve())])
    assert reloaded_cfg.profile.api_key == "new-key"
    assert reloaded_cfg.profile.samplers == {"temperature": 0.7, "nested": {"param": True}}

    # Check that other profile is preserved
    other_profile, _ = load_profile_by_name(config_file, "other")
    assert other_profile.base_url == "http://localhost:9000"
