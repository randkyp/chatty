"""Both streaming transports expose the same provider protocol."""

import asyncio
import json

import httpx
import pytest

from chatty.api import stream_chat, stream_chat_async


@pytest.mark.parametrize("async_transport", [False, True], ids=["cli", "web"])
@pytest.mark.parametrize(
    "response, expected",
    [
        (httpx.Response(400, text="bad request"), [("error", "HTTP 400: bad request")]),
        (
            httpx.Response(
                200,
                text=(
                    'data: {"error": {"message": "failed"}}\n\ndata: {"choices": [{"delta": {"content": "ignored"}}]}\n'
                ),
            ),
            [("error", "Server error: failed")],
        ),
        (
            httpx.Response(
                200,
                text=(
                    ": keepalive\n\ndata: invalid\n\n"
                    'data: {"choices": [{"delta": {"content": "hello"}}], "usage": {"completion_tokens": 1}}\n\n'
                    "data: [DONE]\n\n"
                    'data: {"choices": [{"delta": {"content": "ignored"}}]}\n'
                ),
            ),
            [("usage", '{"completion_tokens": 1}'), ("delta", "hello")],
        ),
        (httpx.ConnectError("offline"), [("error", "Connection failed: offline")]),
        (httpx.ReadError("broken"), [("error", "HTTP error: broken")]),
    ],
)
def test_stream_protocol(respx_mock, async_transport, response, expected):
    route = respx_mock.post("http://up/v1/chat/completions")
    if isinstance(response, Exception):
        route.side_effect = response
    else:
        route.return_value = response
    args = ("http://up", "secret", "model", [{"role": "user", "content": "hi"}], {"temperature": 0.5}, 100)

    async def collect():
        return [event async for event in stream_chat_async(*args)]

    events = asyncio.run(collect()) if async_transport else list(stream_chat(*args))
    assert events == expected
    request = route.calls.last.request
    assert json.loads(request.content) == {
        "model": "model",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": 0.5,
        "max_tokens": 100,
    }
    assert request.headers["Authorization"] == "Bearer secret"
