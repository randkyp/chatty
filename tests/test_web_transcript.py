import pytest
from fastapi.testclient import TestClient

from chatty.config import AppConfig, Profile
from chatty.web import server


def submit(ws, text, **extra):
    ws.send_json({"type": "message", "text": text, **extra})
    events = []
    while True:
        event = ws.receive_json()
        events.append(event)
        if event["type"] == "submission_done":
            return event, events


@pytest.mark.timeout(10)
def test_authoritative_undo_btw_retry_and_failure(tmp_path, monkeypatch, respx_mock):
    server.app.state.cfg = AppConfig(
        config_path=tmp_path / "config.toml",
        profile=Profile(name="test", base_url="http://up", model="test", ctx_size=4096, genmax=256),
    )
    respx_mock.post("http://up/tokenize").respond(404)
    replies = iter(["answer", "aside", "replacement", ""])
    payloads = []

    async def stream(**kwargs):
        payloads.append(kwargs["messages"])
        yield "delta", next(replies)

    monkeypatch.setattr(server, "stream_chat_async", stream)
    with TestClient(server.app).websocket_connect("/ws") as ws:
        ws.receive_json()
        original, _ = submit(ws, "question")
        assert original["accepted"]
        aside, events = submit(ws, "/btw aside question")
        assert aside["content"] == original["content"]
        assert next(e for e in events if e["type"] == "stream_start")["ephemeral"]
        retried, events = submit(ws, "/retry")
        assert retried["content"] == [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "replacement"},
        ]
        assert payloads[-1] == [{"role": "user", "content": "question"}]
        assert next(e for e in events if e["type"] == "history")["content"] == payloads[-1]
        failed, _ = submit(ws, "failed", images=[{"data_url": "data:image/png;base64,AA"}])
        assert not failed["accepted"]
        assert failed["content"] == retried["content"]
        undone, _ = submit(ws, "/undo")
        assert undone["content"] == []
