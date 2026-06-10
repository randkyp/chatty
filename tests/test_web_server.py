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
