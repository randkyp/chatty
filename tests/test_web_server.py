import asyncio
import json
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from chatty.config import AppConfig, Profile
from chatty.web import server


@pytest.fixture
def cfg(tmp_path):
    profile = Profile(
        name="test",
        base_url="http://up",
        model="test-model",
        ctx_size=4096,
        genmax=256,
    )
    return AppConfig(config_path=tmp_path / "config.toml", profile=profile)


@pytest.fixture
def client(cfg):
    server.app.state.cfg = cfg
    return TestClient(server.app)


def _mock_upstream(respx_mock, body):
    # /tokenize is unavailable -> token counting falls back to tiktoken.
    respx_mock.post("http://up/tokenize").respond(404)
    respx_mock.post("http://up/v1/chat/completions").respond(
        200, text=body, headers={"content-type": "text/event-stream"}
    )


def test_get_index(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Chatty" in r.text


def test_build_session_configures_authenticated_token_counter(tmp_path):
    profile = Profile(name="test", base_url="http://up", api_key="test-key")
    cfg = AppConfig(config_path=tmp_path / "config.toml", profile=profile)

    session = server._build_session(cfg)

    assert session._counter.base_url == "http://up"
    assert session._counter.api_key == "test-key"


def test_websocket_welcome_shows_profile_model(tmp_path):
    profile = Profile(name="test", base_url="http://up", model="private-model", ctx_size=4096, genmax=0)
    server.app.state.cfg = AppConfig(config_path=tmp_path / "c.toml", profile=profile)
    with TestClient(server.app).websocket_connect("/ws") as ws:
        welcome = ws.receive_json()
    assert welcome["type"] == "welcome"
    assert "test - private-model" in welcome["content"]


def test_websocket_welcome_uses_auto_when_profile_has_no_model(tmp_path):
    profile = Profile(name="test", base_url="http://up", model=None, ctx_size=4096, genmax=0)
    server.app.state.cfg = AppConfig(config_path=tmp_path / "c.toml", profile=profile)
    with TestClient(server.app).websocket_connect("/ws") as ws:
        welcome = ws.receive_json()
    assert welcome["type"] == "welcome"
    assert "test - (auto)" in welcome["content"]


def test_websocket_streams_response(client, respx_mock):
    _mock_upstream(
        respx_mock,
        'data: {"choices": [{"delta": {"content": "Hello"}}]}\n\n'
        'data: {"choices": [{"delta": {"content": " there"}}]}\n\n'
        "data: [DONE]\n",
    )
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"type": "message", "text": "hi"})

        types, chunks = [], []
        for _ in range(30):
            msg = ws.receive_json()
            types.append(msg["type"])
            if msg["type"] == "stream_chunk":
                chunks.append(msg["content"])
            if msg["type"] == "stream_end":
                break

    assert "stream_start" in types
    assert "stream_end" in types
    assert "".join(chunks) == "Hello there"


def test_websocket_reconnect_restores_history_and_settings(client, respx_mock):
    _mock_upstream(respx_mock, 'data: {"choices": [{"delta": {"content": "answer"}}]}\n\ndata: [DONE]\n')
    session_id = "browser-session-123456"
    client_id = "browser-client-123456"
    url = f"/ws?session_id={session_id}&client_id={client_id}"

    with client.websocket_connect(url) as ws:
        welcome = ws.receive_json()
        assert welcome["restored"] is False
        ws.send_json({"type": "model_select", "model": "changed-model"})
        ws.receive_json()
        ws.send_json({"type": "message", "text": "first"})
        while ws.receive_json()["type"] != "submission_done":
            pass

    with client.websocket_connect(url) as ws:
        welcome = ws.receive_json()
        assert welcome["restored"] is True
        assert welcome["history"] == [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
        ]
        ws.send_json({"type": "message", "text": "second"})
        while ws.receive_json()["type"] != "submission_done":
            pass

    payload = json.loads(
        [call.request for call in respx_mock.calls if call.request.url.path == "/v1/chat/completions"][-1].content
    )
    assert payload["model"] == "changed-model"
    assert payload["messages"] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second"},
    ]


def test_simultaneous_clients_cannot_share_conversation(client, respx_mock):
    _mock_upstream(respx_mock, 'data: {"choices": [{"delta": {"content": "answer"}}]}\n\ndata: [DONE]\n')
    session_id = "shared-session-123456"
    with client.websocket_connect(f"/ws?session_id={session_id}&client_id=first-client-123456") as first:
        first_welcome = first.receive_json()
        first.send_json({"type": "message", "text": "private history"})
        while first.receive_json()["type"] != "submission_done":
            pass

        with client.websocket_connect(f"/ws?session_id={session_id}&client_id=second-client-123456") as second:
            second_welcome = second.receive_json()
            assert second_welcome["session_id"] != first_welcome["session_id"]
            assert second_welcome["restored"] is False
            assert second_welcome["history"] == []

        first.send_json({"type": "message", "text": "still mine"})
        while first.receive_json()["type"] != "submission_done":
            pass

    payload = json.loads(
        [call.request for call in respx_mock.calls if call.request.url.path == "/v1/chat/completions"][-1].content
    )
    assert [message["content"] for message in payload["messages"]] == ["private history", "answer", "still mine"]


def test_web_autosave_saves_session_on_disconnect(client, respx_mock, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client.app.state.cfg.autosave = True
    _mock_upstream(respx_mock, 'data: {"choices": [{"delta": {"content": "saved"}}]}\n\ndata: [DONE]\n')

    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "message", "text": "save on disconnect"})
        while ws.receive_json()["type"] != "submission_done":
            pass

    path = tmp_path / ".config" / "chatty" / "session.json"
    assert json.loads(path.read_text())["messages"] == [
        {"role": "user", "content": "save on disconnect"},
        {"role": "assistant", "content": "saved"},
    ]


def test_web_autosave_saves_current_session_on_exit(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg.autosave = True
    monkeypatch.setattr(server, "load_config", lambda argv: cfg)
    monkeypatch.setattr(server, "resolve_limits", lambda config: None)

    def serve_then_exit(app, **kwargs):
        state, _ = server._acquire_web_session(cfg, "autosave-session-123456", "autosave-client-123456")
        state.chat.add_user_message("save me")

    monkeypatch.setattr("uvicorn.run", serve_then_exit)
    server.run_server(["--web", "--autosave"])

    path = tmp_path / ".config" / "chatty" / "session.json"
    assert json.loads(path.read_text())["messages"] == [{"role": "user", "content": "save me"}]


def test_websocket_command(client, respx_mock):
    _mock_upstream(respx_mock, "data: [DONE]\n")
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # welcome
        ws.send_json({"type": "message", "text": "/help"})
        seen = []
        for _ in range(10):
            msg = ws.receive_json()
            seen.append(msg)
            if msg["type"] == "system":  # the /help output ends the exchange
                break
    types = [m["type"] for m in seen]
    assert "command_start" in types
    assert "command_end" in types
    assert any(m["type"] == "system" and "/help" in m["content"] for m in seen)


def test_websocket_model_picker_selects_and_next_completion_uses_model(client, respx_mock):
    respx_mock.get("http://up/v1/models").respond(
        200,
        json={"data": [{"id": "Zulu"}, {"id": "alpha"}, {"id": "Zulu"}, {"id": ""}]},
    )
    _mock_upstream(respx_mock, 'data: {"choices": [{"delta": {"content": "ok"}}]}\n\ndata: [DONE]\n')

    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "message", "text": "/models"})
        messages = [ws.receive_json() for _ in range(3)]
        assert [message["type"] for message in messages] == ["command_start", "command_end", "model_picker"]
        assert messages[-1] == {
            "type": "model_picker",
            "content": ["alpha", "Zulu"],
            "current": "test-model",
        }

        assert ws.receive_json()["type"] == "submission_done"
        ws.send_json({"type": "model_select", "model": "alpha"})
        assert ws.receive_json() == {"type": "model_selected", "content": "Model: alpha"}

        ws.send_json({"type": "message", "text": "hello"})
        for _ in range(10):
            if ws.receive_json()["type"] == "stream_end":
                break

    chat_request = next(call.request for call in respx_mock.calls if call.request.url.path == "/v1/chat/completions")
    assert json.loads(chat_request.content)["model"] == "alpha"


def test_websocket_single_model_still_emits_picker(client, respx_mock):
    respx_mock.get("http://up/v1/models").respond(200, json={"data": [{"id": "only"}]})

    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "message", "text": "/models"})
        messages = [ws.receive_json() for _ in range(3)]

    assert messages[-1]["type"] == "model_picker"
    assert messages[-1]["content"] == ["only"]


def test_websocket_cancel_without_generation_is_noop(client, respx_mock):
    _mock_upstream(respx_mock, "data: [DONE]\n")
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # welcome
        ws.send_json({"type": "cancel"})  # nothing running; must not crash
        ws.send_json({"type": "message", "text": "/help"})
        # Connection still responsive after a stray cancel.
        assert any(ws.receive_json()["type"] == "command_start" for _ in range(3))


@pytest.mark.timeout(10)
@pytest.mark.parametrize("partial", [False, True])
def test_stop_closes_silent_stream_and_allows_next_request(client, respx_mock, partial):
    reading = threading.Event()
    closed = threading.Event()

    class SilentStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            if partial:
                yield b'data: {"choices": [{"delta": {"content": "partial"}}]}\n\n'
            reading.set()
            await asyncio.Event().wait()
            yield b""  # pragma: no cover

        async def aclose(self):
            closed.set()

    respx_mock.post("http://up/tokenize").respond(404)
    route = respx_mock.post("http://up/v1/chat/completions")
    route.return_value = httpx.Response(200, stream=SilentStream())
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "message", "text": "first"})
        assert ws.receive_json()["type"] == "history"
        assert ws.receive_json()["type"] == "stream_start"
        if partial:
            assert ws.receive_json()["content"] == "partial"
        assert reading.wait(3)
        ws.send_json({"type": "message", "text": "rejected while busy"})
        assert ws.receive_json()["type"] == "submission_rejected"
        ws.send_json({"type": "model_select", "model": "next-model"})
        assert ws.receive_json() == {"type": "model_selected", "content": "Model: next-model"}
        ws.send_json({"type": "cancel"})
        assert ws.receive_json()["type"] == "stream_end"
        assert closed.wait(3)
        done = ws.receive_json()
        assert done["type"] == "submission_done"
        assert done["accepted"] is partial

        route.respond(200, text='data: {"choices": [{"delta": {"content": "next"}}]}\n\ndata: [DONE]\n')
        ws.send_json({"type": "message", "text": "second"})
        assert ws.receive_json()["type"] == "history"
        assert ws.receive_json()["type"] == "stream_start"
        assert ws.receive_json()["content"] == "next"
        assert ws.receive_json()["type"] == "stream_end"

    payload = json.loads(route.calls[-1].request.content)
    assert payload["model"] == "next-model"
    assert payload["messages"] == (
        [{"role": "user", "content": "first"}, {"role": "assistant", "content": "partial"}] if partial else []
    ) + [{"role": "user", "content": "second"}]


@pytest.mark.timeout(10)
def test_disconnect_closes_silent_stream(client, respx_mock):
    reading = threading.Event()
    closed = threading.Event()

    class SilentStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            reading.set()
            await asyncio.Event().wait()
            yield b""  # pragma: no cover

        async def aclose(self):
            closed.set()

    respx_mock.post("http://up/tokenize").respond(404)
    respx_mock.post("http://up/v1/chat/completions").return_value = httpx.Response(200, stream=SilentStream())
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "message", "text": "first"})
        assert ws.receive_json()["type"] == "history"
        assert ws.receive_json()["type"] == "stream_start"
        assert reading.wait(3)
    assert closed.wait(3)


@pytest.mark.timeout(10)
def test_payload_tokenization_does_not_block_websocket_loop(client, respx_mock, monkeypatch):
    """Slow token counting during payload build must not stall Stop or other frames."""
    from chatty.chat_session import TokenCounter

    tokenize_started = threading.Event()
    tokenize_release = threading.Event()
    original_count = TokenCounter.count

    def slow_count(self, text):
        if text:
            tokenize_started.set()
            assert tokenize_release.wait(5)
        return original_count(self, text)

    monkeypatch.setattr(TokenCounter, "count", slow_count)
    _mock_upstream(respx_mock, 'data: {"choices": [{"delta": {"content": "ok"}}]}\n\ndata: [DONE]\n')

    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # welcome
        ws.send_json({"type": "message", "text": "hello"})
        assert tokenize_started.wait(3)

        # Event loop is free while tokenization runs in a worker thread.
        ws.send_json({"type": "model_select", "model": "other-model"})
        assert ws.receive_json() == {"type": "model_selected", "content": "Model: other-model"}
        ws.send_json({"type": "cancel"})  # receivable mid-tokenize (no-op if stream not started)

        tokenize_release.set()
        seen = []
        for _ in range(20):
            message = ws.receive_json()
            seen.append(message["type"])
            if message["type"] == "submission_done":
                break
        assert "stream_end" in seen
        assert "submission_done" in seen


def test_websocket_ephemeral_key_authenticates_tokenize_and_chat(tmp_path, respx_mock):
    profile = Profile(
        name="private",
        base_url="http://up",
        api_key_mode="ephemeral",
        model="m",
        ctx_size=4096,
        genmax=0,
    )
    cfg = AppConfig(config_path=tmp_path / "config.toml", profile=profile)
    server.app.state.cfg = cfg
    respx_mock.post("http://up/tokenize").respond(200, json={"tokens": [1]})
    respx_mock.post("http://up/v1/chat/completions").respond(
        200,
        text='data: {"choices": [{"delta": {"content": "ok"}}]}\n\ndata: [DONE]\n',
        headers={"content-type": "text/event-stream"},
    )

    with TestClient(server.app).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "welcome"
        assert ws.receive_json()["type"] == "api_key_prompt"
        ws.send_json({"type": "api_key", "api_key": "old-secret"})
        assert "set" in ws.receive_json()["content"]
        ws.send_json({"type": "api_key", "api_key": "runtime-secret"})
        assert "set" in ws.receive_json()["content"]
        ws.send_json({"type": "message", "text": "hello"})
        for _ in range(10):
            if ws.receive_json()["type"] == "stream_end":
                break

    assert cfg.effective_api_key == "runtime-secret"
    upstream = [call.request for call in respx_mock.calls]
    assert {request.url.path for request in upstream} == {"/tokenize", "/v1/chat/completions"}
    assert all(request.headers["Authorization"] == "Bearer runtime-secret" for request in upstream)


def test_websocket_key_is_shared_with_existing_connection_and_reconnect(tmp_path, respx_mock):
    profile = Profile(
        name="private",
        base_url="http://up",
        api_key_mode="ephemeral",
        model="m",
        ctx_size=4096,
        genmax=0,
    )
    cfg = AppConfig(config_path=tmp_path / "config.toml", profile=profile)
    server.app.state.cfg = cfg
    respx_mock.post("http://up/tokenize").respond(200, json={"tokens": [1]})
    respx_mock.post("http://up/v1/chat/completions").respond(
        200,
        text='data: {"choices": [{"delta": {"content": "ok"}}]}\n\ndata: [DONE]\n',
        headers={"content-type": "text/event-stream"},
    )

    with TestClient(server.app) as test_client:
        with test_client.websocket_connect("/ws") as first, test_client.websocket_connect("/ws") as second:
            assert first.receive_json()["type"] == "welcome"
            assert first.receive_json()["type"] == "api_key_prompt"
            assert second.receive_json()["type"] == "welcome"
            assert second.receive_json()["type"] == "api_key_prompt"
            first.send_json({"type": "api_key", "api_key": "shared-secret"})
            first.receive_json()
            second.send_json({"type": "message", "text": "hello"})
            for _ in range(10):
                if second.receive_json()["type"] == "stream_end":
                    break
            chat_request = next(
                call.request for call in respx_mock.calls if call.request.url.path == "/v1/chat/completions"
            )
            assert chat_request.headers["Authorization"] == "Bearer shared-secret"

        with test_client.websocket_connect("/ws") as reconnected:
            reconnected.receive_json()
            reconnected.send_json({"type": "message", "text": "/apikey status"})
            seen = [reconnected.receive_json() for _ in range(3)]
            assert any(message["type"] == "system" and "is set" in message["content"] for message in seen)
            assert reconnected.receive_json()["type"] == "submission_done"
            reconnected.send_json({"type": "message", "text": "/apikey clear"})
            seen = [reconnected.receive_json() for _ in range(3)]
            assert any(message["type"] == "system" and "cleared" in message["content"] for message in seen)
            assert cfg.effective_api_key is None


def test_websocket_rejects_forged_key_event_for_stored_profile(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "api_key", "api_key": "forged-secret"})
        response = ws.receive_json()
    assert response["type"] == "error"
    assert "only available" in response["content"]


def test_websocket_bare_apikey_requests_password_control(tmp_path):
    profile = Profile(name="private", base_url="http://up", api_key_mode="ephemeral")
    server.app.state.cfg = AppConfig(config_path=tmp_path / "config.toml", profile=profile)
    with TestClient(server.app).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "welcome"
        assert ws.receive_json()["type"] == "api_key_prompt"
        ws.send_json({"type": "message", "text": "/apikey"})
        seen = [ws.receive_json() for _ in range(3)]
    assert [message["type"] for message in seen] == ["command_start", "command_end", "api_key_prompt"]


def test_websocket_profile_switch_preprompts_for_missing_key(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[profile.default]\nbase_url = "http://up"\n'
        '[profile.private]\nbase_url = "http://private"\napi_key_mode = "ephemeral"\n'
    )
    server.app.state.cfg = AppConfig(
        config_path=config_path,
        profile=Profile(name="default", base_url="http://up"),
    )
    with TestClient(server.app).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "welcome"
        ws.send_json({"type": "message", "text": "/profile private"})
        seen = [ws.receive_json() for _ in range(4)]
    assert [message["type"] for message in seen] == [
        "command_start",
        "command_end",
        "system",
        "api_key_prompt",
    ]
    assert "Switched to profile 'private'" in seen[2]["content"]


def test_websocket_missing_ephemeral_key_never_calls_upstream(tmp_path, respx_mock):
    profile = Profile(
        name="private",
        base_url="http://up",
        api_key_mode="ephemeral",
        model="m",
        ctx_size=4096,
        genmax=0,
    )
    server.app.state.cfg = AppConfig(config_path=tmp_path / "config.toml", profile=profile)

    with TestClient(server.app).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "welcome"
        assert ws.receive_json()["type"] == "api_key_prompt"
        ws.send_json({"type": "message", "text": "hello"})
        response = ws.receive_json()
    assert response["type"] == "error"
    assert "/apikey" in response["content"]
    assert not respx_mock.calls


def test_web_assets_use_separate_non_persistent_password_control(client):
    script = client.get("/static/app.js").text
    assert "keyInput.type = 'password'" in script
    assert "type: 'api_key'" in script
    assert "inputHistory.push(apiKey)" not in script
    assert "localStorage.setItem(key, JSON.stringify(value))" in script
    assert script.index("const apiKeyCommand") < script.index("inputHistory.push(text)")
    assert script.index("keyInput.value = ''") < script.index("type: 'api_key'")


def test_web_assets_implement_accessible_transient_model_picker(client):
    script = client.get("/static/app.js").text
    styles = client.get("/static/styles.css").text

    assert "role', 'combobox'" in script
    assert "role', 'listbox'" in script
    assert "aria-activedescendant" in script
    assert "No matching models" in script
    assert "if (modelPicker) return;" in script
    assert "closeModelPickerLocal(true);" in script
    assert "max-height: calc(8 * 1.5em + 0.5em)" in styles
