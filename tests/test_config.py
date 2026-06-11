import pytest

from chatty.config import (
    DEFAULT_CONFIG,
    ProfileNotFoundError,
    _ensure_config,
    _load_profile,
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


def test_load_profile_missing_base_url():
    raw_toml = {"profile": {"default": {"api_key": "testkey"}}}
    with pytest.raises(ProfileNotFoundError, match="missing required field 'base_url'"):
        _load_profile(raw_toml, "default")


def test_load_profile_missing_profile():
    raw_toml = {"profile": {"good": {"base_url": "http://x"}}}
    with pytest.raises(ProfileNotFoundError) as excinfo:
        _load_profile(raw_toml, "nonexistent")
    assert excinfo.value.available == ["good"]


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


# --- switch_profile / resolve_limits / save_profile None-sampler skip ---


def test_switch_profile(tmp_path):
    from chatty.config import AppConfig, Profile

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[profile.a]\nbase_url = "http://a"\nmodel = "ma"\n[profile.b]\nbase_url = "http://b"\nmodel = "mb"\n'
    )
    cfg = AppConfig(config_path=cfg_path, profile=Profile(name="a", base_url="http://a"))
    cfg.switch_profile("b")
    assert cfg.profile.name == "b"
    assert cfg.profile.model == "mb"
    assert cfg._raw["profile"]["b"]["base_url"] == "http://b"


def test_switch_profile_missing_raises(tmp_path):
    from chatty.config import AppConfig, Profile

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[profile.a]\nbase_url = "http://a"\n')
    cfg = AppConfig(config_path=cfg_path, profile=Profile(name="a", base_url="http://a"))
    with pytest.raises(ProfileNotFoundError):
        cfg.switch_profile("nope")


def test_resolve_limits_uses_metadata(respx_mock):
    from chatty.config import AppConfig, Profile, resolve_limits

    respx_mock.get("http://up/v1/models").respond(
        200, json={"data": [{"id": "m1", "context_length": 4096, "max_tokens": 512}]}
    )
    cfg = AppConfig(config_path=None, profile=Profile(name="p", base_url="http://up"))
    resolve_limits(cfg)
    assert cfg.profile.ctx_size == 4096
    assert cfg.profile.genmax == 512
    assert cfg.profile.model == "m1"


def test_save_profile_skips_none_sampler(tmp_path):
    from chatty.config import AppConfig, Profile

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[profile.default]\nbase_url = "http://x"\n')
    p = Profile(name="default", base_url="http://x", samplers={"temperature": 0.7, "stop": None})
    cfg = AppConfig(config_path=cfg_path, profile=p)
    save_profile(cfg)  # must not raise on the None value
    text = cfg_path.read_text()
    assert "temperature" in text
    assert "stop" not in text
