import httpx

from chatty.api import _auth_headers, fetch_model_metadata, stream_chat


def test_auth_headers():
    assert _auth_headers(None) == {"Content-Type": "application/json"}
    assert _auth_headers("mykey") == {"Content-Type": "application/json", "Authorization": "Bearer mykey"}


def test_fetch_model_metadata_success(respx_mock):
    # Mock /v1/models response
    respx_mock.get("http://localhost:8080/v1/models").respond(
        status_code=200, json={"data": [{"id": "my-cool-model", "context_length": 8192, "max_tokens": 1024}]}
    )

    meta = fetch_model_metadata("http://localhost:8080", "testkey")
    assert meta == {"model_id": "my-cool-model", "ctx_size": 8192, "genmax": 1024}


def test_fetch_model_metadata_missing_fields(respx_mock):
    respx_mock.get("http://localhost:8080/v1/models").respond(status_code=200, json={"data": [{"id": "model"}]})

    meta = fetch_model_metadata("http://localhost:8080", None)
    assert meta == {"model_id": "model"}


def test_fetch_model_metadata_error(respx_mock):
    respx_mock.get("http://localhost:8080/v1/models").respond(status_code=500)

    meta = fetch_model_metadata("http://localhost:8080", None)
    assert meta == {}


def test_stream_chat_success(respx_mock):
    # Mock streaming response
    streaming_content = (
        'data: {"choices": [{"delta": {"content": "Hello"}}]}\n\n'
        'data: {"choices": [{"delta": {"content": " world!"}}]}\n\n'
        "data: [DONE]\n"
    )
    respx_mock.post("http://localhost:8080/v1/chat/completions").respond(status_code=200, content=streaming_content)

    deltas = list(
        stream_chat(
            base_url="http://localhost:8080",
            api_key="key",
            model="gpt-3.5",
            messages=[{"role": "user", "content": "hi"}],
            samplers={"temperature": 0.7},
            genmax=100,
        )
    )

    assert deltas == [("delta", "Hello"), ("delta", " world!")]


def test_stream_chat_http_error(respx_mock):
    respx_mock.post("http://localhost:8080/v1/chat/completions").respond(status_code=400, content="Bad Request")

    deltas = list(
        stream_chat(
            base_url="http://localhost:8080", api_key=None, model="gpt-3.5", messages=[], samplers={}, genmax=100
        )
    )

    assert len(deltas) == 1
    assert deltas[0][0] == "error"
    assert "HTTP 400: Bad Request" in deltas[0][1]


def test_stream_chat_connection_error(respx_mock):
    respx_mock.post("http://localhost:8080/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("Connection timed out")
    )

    deltas = list(
        stream_chat(
            base_url="http://localhost:8080", api_key=None, model="gpt-3.5", messages=[], samplers={}, genmax=100
        )
    )

    assert len(deltas) == 1
    assert deltas[0][0] == "error"
    assert "Connection failed:" in deltas[0][1]
