import pytest
from chatty.chat_session import ChatSession
from chatty.commands import (
    _del_nested,
    _parse_value,
    _set_nested,
    handle_command,
)
from chatty.config import AppConfig, Profile

# --- Helper tests ---


def test_parse_value():
    assert _parse_value("true") is True
    assert _parse_value("False") is False
    assert _parse_value("null") is None
    assert _parse_value("None") is None
    assert _parse_value("123") == 123
    assert _parse_value("12.34") == 12.34
    assert _parse_value("hello") == "hello"


def test_set_nested():
    d = {}
    _set_nested(d, "temperature", 0.7)
    assert d == {"temperature": 0.7}

    _set_nested(d, "nested.param", True)
    assert d == {"temperature": 0.7, "nested": {"param": True}}

    _set_nested(d, "nested.sub.val", 42)
    assert d == {"temperature": 0.7, "nested": {"param": True, "sub": {"val": 42}}}


def test_del_nested():
    d = {"temperature": 0.7, "nested": {"param": True, "sub": {"val": 42}}}

    assert _del_nested(d, "temperature") is True
    assert "temperature" not in d

    assert _del_nested(d, "nested.param") is True
    assert "param" not in d["nested"]

    assert _del_nested(d, "nested.nonexistent") is False
    assert _del_nested(d, "nonexistent.sub") is False


# --- Command Dispatch tests ---


@pytest.fixture
def session():
    return ChatSession(system_prompt="Initial Sys", ctx_size=4096, genmax=256)


@pytest.fixture
def app_config(tmp_path):
    p = Profile(
        name="default",
        base_url="http://localhost:8080",
        system_prompt="Initial Sys",
        ctx_size=4096,
        genmax=256,
        samplers={"temperature": 0.7},
    )
    return AppConfig(
        config_path=tmp_path / "config.toml",
        profile=p,
        debug=False,
    )


def test_cmd_quit_exit(session, app_config):
    res1 = handle_command("/quit", session, app_config)
    assert res1.quit is True

    res2 = handle_command("/exit", session, app_config)
    assert res2.quit is True


def test_cmd_clear(session, app_config):
    session.add_user_message("hello")
    res = handle_command("/clear", session, app_config)
    assert res.message == "History cleared."
    assert len(session.messages) == 0


def test_cmd_undo(session, app_config):
    session.add_user_message("hello")
    session.add_assistant_message("hi")

    res = handle_command("/undo", session, app_config)
    assert res.message == "Last exchange removed."
    assert len(session.messages) == 0

    res_empty = handle_command("/undo", session, app_config)
    assert res_empty.message == "Nothing to undo."


def test_cmd_system(session, app_config):
    # Show system prompt
    res = handle_command("/system", session, app_config)
    assert "System prompt: Initial Sys" in res.message

    # Set system prompt
    res_set = handle_command("/system New Sys Prompt", session, app_config)
    assert res_set.message == "System prompt updated."
    assert session.system_prompt == "New Sys Prompt"
    assert app_config.profile.system_prompt == "New Sys Prompt"

    # Disable system prompt
    res_disable = handle_command("/system disable", session, app_config)
    assert res_disable.message == "System prompt disabled."
    assert session.system_prompt is None
    assert app_config.profile.system_prompt is None


def test_cmd_ctx(session, app_config):
    # Show info
    res = handle_command("/ctx", session, app_config)
    assert "Context window : 4096 tokens" in res.message

    # Set ctx size
    res_set = handle_command("/ctx 2048", session, app_config)
    assert res_set.message == "Context size set to 2048."
    assert session.ctx_size == 2048


def test_cmd_genmax(session, app_config):
    # Show info
    res = handle_command("/genmax", session, app_config)
    assert "Max generation tokens: 256" in res.message

    # Set genmax
    res_set = handle_command("/genmax 512", session, app_config)
    assert res_set.message == "Max generation tokens set to 512."
    assert session.genmax == 512
    assert app_config.profile.genmax == 512


def test_cmd_profile(session, app_config, tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("""
[profile.default]
base_url = "http://localhost:8080"

[profile.custom]
base_url = "http://localhost:9000"
system_prompt = "Custom Sys"
ctx_size = 8192
genmax = 0
""")
    app_config.config_path = config_file

    res = handle_command("/profile custom", session, app_config)
    assert "Switched to profile 'custom'" in res.message
    assert app_config.profile.name == "custom"
    assert app_config.profile.base_url == "http://localhost:9000"
    assert session.system_prompt == "Custom Sys"
    assert session.ctx_size == 8192
    assert session.genmax == 0


def test_cmd_samplers(session, app_config):
    # Show samplers
    res = handle_command("/samplers show", session, app_config)
    assert "temperature" in res.message

    # Set sampler
    res_set = handle_command("/samplers top_p 0.9", session, app_config)
    assert "Set sampler 'top_p' = 0.9" in res_set.message
    assert app_config.profile.samplers["top_p"] == 0.9

    # Remove sampler
    res_rm = handle_command("/samplers rm top_p", session, app_config)
    assert "Removed sampler 'top_p'" in res_rm.message
    assert "top_p" not in app_config.profile.samplers

    # Disable all samplers
    res_disable = handle_command("/samplers disable", session, app_config)
    assert "All samplers cleared" in res_disable.message
    assert len(app_config.profile.samplers) == 0


def test_cmd_save_and_load_session(session, app_config, tmp_path):
    session.system_prompt = "SaveSys"
    session.ctx_size = 5000
    session.genmax = 400
    session.add_user_message("Persisted Msg")
    session.add_assistant_message("Persisted Reply")

    save_file = tmp_path / "test_session.json"

    # Save session
    res_save = handle_command(f"/save {save_file}", session, app_config)
    assert "Session saved to" in res_save.message
    assert save_file.exists()

    # Load session back into empty session
    new_session = ChatSession()
    res_load = handle_command(f"/load {save_file}", new_session, app_config)
    assert "Session restored from" in res_load.message
    assert new_session.system_prompt == "SaveSys"
    assert new_session.ctx_size == 5000
    assert new_session.genmax == 400
    assert len(new_session.messages) == 2
    assert new_session.messages[0]["content"] == "Persisted Msg"


def test_cmd_image_file_not_found(session, app_config):
    res = handle_command("/image /nonexistent/file.png", session, app_config)
    assert "File not found" in res.message


def test_cmd_models_switch(session, app_config):
    res = handle_command("/models gpt-4", session, app_config)
    assert "Switched to model 'gpt-4'" in res.message
    assert app_config.profile.model == "gpt-4"


def test_cmd_models_list(session, app_config, monkeypatch):
    import chatty.api

    monkeypatch.setattr(chatty.api, "list_models", lambda b, a: ["model-1", "model-2"])
    res = handle_command("/models", session, app_config)
    assert "model-1" in res.message
    assert "model-2" in res.message


def test_cmd_btw(session, app_config):
    res_empty = handle_command("/btw", session, app_config)
    assert "Usage: /btw <message>" in res_empty.message

    res = handle_command("/btw what is 2+2?", session, app_config)
    assert res.ephemeral_prompt == "what is 2+2?"
    assert res.message is None
    assert res.quit is False
