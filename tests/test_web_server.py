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


def test_websocket_welcome_uses_auto_for_no_model(tmp_path, respx_mock):
    # Regression: web welcome must not interpolate a literal "None" model.
    profile = Profile(name="test", base_url="http://up", model=None, ctx_size=4096, genmax=0)
    server.app.state.cfg = AppConfig(config_path=tmp_path / "c.toml", profile=profile)
    with TestClient(server.app).websocket_connect("/ws") as ws:
        welcome = ws.receive_json()
    assert welcome["type"] == "welcome"
    assert "None" not in welcome["content"]
    assert "(auto)" in welcome["content"]


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


def test_websocket_cancel_without_generation_is_noop(client, respx_mock):
    _mock_upstream(respx_mock, "data: [DONE]\n")
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()  # welcome
        ws.send_json({"type": "cancel"})  # nothing running; must not crash
        ws.send_json({"type": "message", "text": "/help"})
        # Connection still responsive after a stray cancel.
        assert any(ws.receive_json()["type"] == "command_start" for _ in range(3))


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
