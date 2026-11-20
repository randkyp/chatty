"""OpenCode session affinity: x-opencode-session, client UA, and id lifetime."""

import pytest
import tomlkit
from fastapi.testclient import TestClient

from chatty.api import (
    OPENCODE_SESSION_HEADER,
    USER_AGENT,
    _auth_headers,
    fetch_model_metadata,
    list_models,
    stream_chat,
)
from chatty.chat_session import ChatSession, TokenCounter, new_session_id
from chatty.commands import handle_command, sync_token_counter
from chatty.config import DEFAULT_CONFIG, AppConfig, Profile, load_config, save_profile
from chatty.web import server


class _Reply:
    """One SSE chunk so streaming paths have something to consume."""

    BODY = 'data: {"choices": [{"delta": {"content": "ok"}}]}\n\ndata: [DONE]\n'


# ── Header construction ────────────────────────────────────────────────────


def test_disabled_profile_sends_no_opencode_headers():
    assert _auth_headers("key") == {"Content-Type": "application/json", "Authorization": "Bearer key"}


def test_enabled_profile_sends_ua_and_session_header():
    headers = _auth_headers("key", "ses_abc")
    assert headers["User-Agent"] == USER_AGENT
    assert headers[OPENCODE_SESSION_HEADER] == "ses_abc"
    assert headers["Authorization"] == "Bearer key"


def test_user_agent_identifies_chatty():
    assert USER_AGENT.startswith("chatty/")


def test_chat_request_carries_session_header(respx_mock):
    respx_mock.post("http://up/v1/chat/completions").respond(
        200, text=_Reply.BODY, headers={"content-type": "text/event-stream"}
    )

    list(stream_chat("http://up", None, "m", [{"role": "user", "content": "x"}], {}, 0, opencode_session="ses_x"))

    request = respx_mock.calls.last.request
    assert request.headers[OPENCODE_SESSION_HEADER] == "ses_x"
    assert request.headers["user-agent"] == USER_AGENT


def test_chat_request_omits_session_header_when_disabled(respx_mock):
    respx_mock.post("http://up/v1/chat/completions").respond(
        200, text=_Reply.BODY, headers={"content-type": "text/event-stream"}
    )

    list(stream_chat("http://up", None, "m", [{"role": "user", "content": "x"}], {}, 0))

    assert OPENCODE_SESSION_HEADER not in respx_mock.calls.last.request.headers


def test_auxiliary_requests_carry_session_header(respx_mock):
    respx_mock.get("http://up/v1/models").respond(200, json={"data": [{"id": "m"}]})

    list_models("http://up", None, "ses_aux")
    assert respx_mock.calls.last.request.headers[OPENCODE_SESSION_HEADER] == "ses_aux"

    fetch_model_metadata("http://up", None, "m", "ses_aux")
    assert respx_mock.calls.last.request.headers[OPENCODE_SESSION_HEADER] == "ses_aux"


def test_tokenize_request_carries_session_header(respx_mock):
    respx_mock.post("http://up/tokenize").respond(200, json={"tokens": [1]})

    counter = TokenCounter(base_url="http://up", opencode_session="ses_tok")
    assert counter.count("hello") == 1
    assert respx_mock.calls.last.request.headers[OPENCODE_SESSION_HEADER] == "ses_tok"


def test_debug_prints_headers_with_redacted_key(respx_mock, capsys):
    respx_mock.post("http://up/v1/chat/completions").respond(
        200, text=_Reply.BODY, headers={"content-type": "text/event-stream"}
    )

    list(
        stream_chat(
            "http://up", "sekrit", "m", [{"role": "user", "content": "x"}], {}, 0, debug=True, opencode_session="ses_d"
        )
    )

    out = capsys.readouterr().out
    assert OPENCODE_SESSION_HEADER in out
    assert "Bearer ***" in out
    assert "sekrit" not in out


# ── Conversation identity ──────────────────────────────────────────────────


def test_new_session_id_is_namespaced_and_unique():
    first, second = new_session_id(), new_session_id()
    assert first.startswith("ses_")
    assert len(first) > len("ses_") + 16
    assert first != second


def test_chat_sessions_default_to_distinct_ids():
    assert ChatSession().conversation_id != ChatSession().conversation_id


def test_setting_a_different_system_prompt_starts_a_new_conversation():
    session = ChatSession(system_prompt="old")
    before = session.conversation_id

    session.set_system_prompt("new")

    assert session.system_prompt == "new"
    assert session.conversation_id != before


def test_setting_the_same_system_prompt_keeps_the_conversation():
    session = ChatSession(system_prompt="same")
    before = session.conversation_id

    session.set_system_prompt("same")

    assert session.conversation_id == before


def test_clearing_the_system_prompt_starts_a_new_conversation():
    session = ChatSession(system_prompt="old")
    before = session.conversation_id

    session.set_system_prompt(None)

    assert session.system_prompt is None
    assert session.conversation_id != before


def test_clear_starts_a_new_conversation():
    session = ChatSession()
    session.add_user_message("hello")
    before = session.conversation_id

    session.clear()

    assert session.messages == []
    assert session.conversation_id != before


# ── Config surface ─────────────────────────────────────────────────────────


def test_profile_parses_opencode_session():
    raw = {"profile": {"p": {"base_url": "http://up", "opencode_session": True}}}
    from chatty.config import _load_profile

    assert _load_profile(raw, "p").opencode_session is True
    raw["profile"]["p"]["opencode_session"] = False
    assert _load_profile(raw, "p").opencode_session is False


def test_profile_rejects_non_boolean_opencode_session():
    from chatty.config import ProfileNotFoundError, _load_profile

    raw = {"profile": {"p": {"base_url": "http://up", "opencode_session": "yes"}}}
    with pytest.raises(ProfileNotFoundError, match="invalid opencode_session"):
        _load_profile(raw, "p")


def test_request_session_id_respects_the_opt_in(tmp_path):
    off = AppConfig(config_path=tmp_path / "c.toml", profile=Profile(name="p", base_url="http://up"))
    assert off.request_session_id("ses_chat") is None

    on = AppConfig(
        config_path=tmp_path / "c.toml",
        profile=Profile(name="p", base_url="http://up", opencode_session=True),
    )
    assert on.request_session_id("ses_chat") == "ses_chat"
    fallback = on.request_session_id()
    assert fallback == on.request_session_id()
    assert fallback.startswith("ses_")


def test_save_profile_writes_and_removes_opencode_session(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text('[profile.default]\nbase_url = "http://up"\n')
    cfg = load_config(["-c", str(config_file.resolve())])

    cfg.profile.opencode_session = True
    save_profile(cfg)
    assert "opencode_session = true" in config_file.read_text()
    assert load_config(["-c", str(config_file.resolve())]).profile.opencode_session is True

    cfg.profile.opencode_session = False
    save_profile(cfg)
    assert "opencode_session" not in config_file.read_text()


def test_default_config_keeps_latex_out_of_samplers():
    doc = tomlkit.loads(DEFAULT_CONFIG)
    default = doc["profile"]["default"]
    assert "latex" not in default.get("samplers", {})
    assert "opencode_session" in DEFAULT_CONFIG


def test_sync_token_counter_propagates_session_and_rebuilds_on_toggle(tmp_path):
    cfg = AppConfig(
        config_path=tmp_path / "c.toml",
        profile=Profile(name="p", base_url="http://up", opencode_session=True),
    )
    session = ChatSession()
    sync_token_counter(session, cfg)
    assert session._counter.opencode_session == cfg.request_session_id()

    cfg.profile.opencode_session = False
    sync_token_counter(session, cfg)
    assert session._counter.opencode_session is None


def test_cli_stream_passes_the_conversation_id(monkeypatch, tmp_path):
    from chatty import main

    captured = {}

    def fake_stream(**kwargs):
        captured.update(kwargs)
        return iter(())

    monkeypatch.setattr(main, "stream_chat", fake_stream)
    cfg = AppConfig(
        config_path=tmp_path / "c.toml",
        profile=Profile(name="p", base_url="http://up", opencode_session=True),
        raw_output=True,
    )
    session = ChatSession()

    main._stream_record_report(session, cfg, [{"role": "user", "content": "hi"}])
    assert captured["opencode_session"] == session.conversation_id

    cfg.profile.opencode_session = False
    main._stream_record_report(session, cfg, [{"role": "user", "content": "hi"}])
    assert captured["opencode_session"] is None


# ── Command paths ──────────────────────────────────────────────────────────


@pytest.fixture
def session():
    return ChatSession(system_prompt="Initial Sys", ctx_size=4096, genmax=256)


@pytest.fixture
def app_config(tmp_path):
    profile = Profile(
        name="default",
        base_url="http://up",
        model="m",
        system_prompt="Initial Sys",
        ctx_size=4096,
        genmax=256,
        opencode_session=True,
    )
    return AppConfig(config_path=tmp_path / "config.toml", profile=profile)


def test_system_command_regenerates_only_on_change(session, app_config):
    before = session.conversation_id
    handle_command("/system brand new", session, app_config)
    assert session.conversation_id != before

    after = session.conversation_id
    handle_command("/system brand new", session, app_config)
    assert session.conversation_id == after

    handle_command("/system", session, app_config)  # display only
    assert session.conversation_id == after

    handle_command("/system disable", session, app_config)
    assert session.conversation_id != after


def test_profile_switch_regenerates_only_when_the_prompt_changes(tmp_path, respx_mock):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[profile.a]\nbase_url = "http://a"\nmodel = "ma"\nctx_size = 4096\ngenmax = 256\nsystem_prompt = "A"\n'
        '[profile.b]\nbase_url = "http://b"\nmodel = "mb"\nctx_size = 4096\ngenmax = 256\nsystem_prompt = "B"\n'
        '[profile.c]\nbase_url = "http://c"\nmodel = "mc"\nctx_size = 4096\ngenmax = 256\nsystem_prompt = "B"\n'
    )
    cfg = load_config(["-c", str(config_file.resolve()), "-p", "a"])
    session = ChatSession(system_prompt="A", ctx_size=4096, genmax=256)

    before = session.conversation_id
    handle_command("/profile b", session, cfg)
    assert session.conversation_id != before

    after = session.conversation_id
    handle_command("/profile c", session, cfg)  # same prompt as b
    assert session.conversation_id == after


def test_model_switch_keeps_the_conversation(session, app_config):
    before = session.conversation_id
    handle_command("/model other-model", session, app_config)
    assert session.conversation_id == before


def test_load_always_starts_a_new_conversation(session, app_config, tmp_path):
    path = tmp_path / "same-prompt.json"
    # The loaded prompt matches the live one, but loading is still a new conversation.
    from chatty.commands import save_session

    save_session(ChatSession(system_prompt="Initial Sys", ctx_size=4096, genmax=256), path)
    before = session.conversation_id

    result = handle_command(f"/load {path}", session, app_config)

    assert "Session restored" in result.message
    assert session.system_prompt == "Initial Sys"
    assert session.conversation_id != before


def test_retry_and_btw_keep_the_conversation(session, app_config):
    before = session.conversation_id
    handle_command("/retry", session, app_config)
    assert session.conversation_id == before
    assert handle_command("/btw aside", session, app_config).ephemeral_prompt == "aside"
    assert session.conversation_id == before


# ── Web surface ────────────────────────────────────────────────────────────


def _web_cfg(tmp_path):
    profile = Profile(
        name="test",
        base_url="http://up",
        model="m",
        ctx_size=4096,
        genmax=256,
        opencode_session=True,
    )
    return AppConfig(config_path=tmp_path / "config.toml", profile=profile)


def _exchange(ws):
    ws.send_json({"type": "message", "text": "hello"})
    for _ in range(10):
        if ws.receive_json()["type"] == "stream_end":
            break


def test_websocket_conversations_get_distinct_session_ids(tmp_path, respx_mock):
    server.app.state.cfg = _web_cfg(tmp_path)
    respx_mock.post("http://up/tokenize").respond(404)
    respx_mock.post("http://up/v1/chat/completions").respond(
        200, text=_Reply.BODY, headers={"content-type": "text/event-stream"}
    )

    with TestClient(server.app).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "welcome"
        _exchange(ws)
    with TestClient(server.app).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "welcome"
        _exchange(ws)

    chat_requests = [call.request for call in respx_mock.calls if call.request.url.path == "/v1/chat/completions"]
    session_ids = {request.headers[OPENCODE_SESSION_HEADER] for request in chat_requests}
    assert len(session_ids) == 2
    assert all(session_id.startswith("ses_") for session_id in session_ids)


def test_websocket_auxiliary_requests_use_the_instance_id(tmp_path, respx_mock):
    cfg = _web_cfg(tmp_path)
    cfg.profile.ctx_size = None
    cfg.profile.genmax = None
    cfg.profile.model = None
    server.app.state.cfg = cfg
    respx_mock.get("http://up/v1/models").respond(200, json={"data": [{"id": "m"}]})

    with TestClient(server.app).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"type": "message", "text": "/models"})
        for _ in range(10):
            if ws.receive_json()["type"] == "command_end":
                break

    assert respx_mock.calls.last.request.headers[OPENCODE_SESSION_HEADER] == cfg._instance_session_id
