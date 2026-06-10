from unittest.mock import MagicMock

import httpx

from chatty.chat_session import ChatSession, TokenCounter

# --- TokenCounter Tests ---


def test_token_counter_server_success(respx_mock):
    # Mock /tokenize endpoint returning tokens
    respx_mock.post("http://localhost:8080/tokenize").respond(status_code=200, json={"tokens": [1, 2, 3, 4, 5]})

    counter = TokenCounter(base_url="http://localhost:8080")
    assert counter.count("hello") == 5
    assert counter._use_server is True


def test_token_counter_server_failure_falls_back_to_tiktoken(respx_mock):
    # Mock /tokenize endpoint returning 404
    respx_mock.post("http://localhost:8080/tokenize").respond(status_code=404)

    counter = TokenCounter(base_url="http://localhost:8080")
    # tiktoken encoding of "hello" (cl100k_base) is 1 token ("hello" -> [15339])
    assert counter.count("hello") == 1
    assert counter._use_server is False


def test_token_counter_transient_failure_falls_back(respx_mock):
    # First request: 200 OK -> sets _use_server = True
    # Second request: NetworkError -> falls back to tiktoken
    route = respx_mock.post("http://localhost:8080/tokenize")
    route.side_effect = [httpx.Response(200, json={"tokens": [1, 2, 3]}), httpx.Response(500)]

    counter = TokenCounter(base_url="http://localhost:8080")
    assert counter.count("hello") == 3
    assert counter._use_server is True

    # Second count encounters transient 500 error, falls back to tiktoken (1 token)
    assert counter.count("hello") == 1


def test_token_counter_count_messages():
    counter = TokenCounter(base_url="http://localhost:8080")
    counter._use_server = False  # force tiktoken fallback

    messages = [
        {"role": "system", "content": "You are helpful."},  # sys prompt: ~4 tokens + 4 overhead = 8
        {"role": "user", "content": "Hello"},  # user: 1 token + 4 overhead = 5
        {
            "role": "assistant",
            "content": [  # complex content with image
                {"type": "text", "text": "Hi"},  # text: 1 token
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},  # image: 1000 tokens
            ],
        },  # 1001 tokens + 4 overhead = 1005
    ]
    # Total: 8 + 5 + 1005 + 2 (reply priming) = 1020 tokens
    assert counter.count_messages(messages) == 1020


# --- ChatSession Tests ---


def test_chat_session_append_simple():
    session = ChatSession()
    session.add_user_message("hello")
    session.add_assistant_message("hi there")

    assert len(session.messages) == 2
    assert session.messages[0] == {"role": "user", "content": "hello"}
    assert session.messages[1] == {"role": "assistant", "content": "hi there"}


def test_chat_session_append_consecutive_concatenation():
    session = ChatSession()
    session.add_user_message("hello")
    session.add_user_message("how are you?")

    assert len(session.messages) == 1
    assert session.messages[0] == {"role": "user", "content": "hello\nhow are you?"}


def test_chat_session_append_consecutive_concatenation_with_images():
    session = ChatSession()
    session.add_user_message("hello")
    session.add_user_message("look at this", images=[{"data_url": "data:image/png;base64,123"}])

    assert len(session.messages) == 1
    expected_content = [
        {"type": "text", "text": "hello"},
        {"type": "text", "text": "look at this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,123"}},
    ]
    assert session.messages[0] == {"role": "user", "content": expected_content}


def test_chat_session_clear():
    session = ChatSession(system_prompt="Always be polite")
    session.add_user_message("hello")
    session.pending_images.append({"data_url": "foo"})

    session.clear()
    assert len(session.messages) == 0
    assert len(session.pending_images) == 0
    assert session.system_prompt == "Always be polite"  # system prompt is preserved


def test_chat_session_undo():
    session = ChatSession()
    assert session.undo() is False

    session.add_user_message("hello")
    session.add_assistant_message("hi")

    # pops both assistant and user
    assert session.undo() is True
    assert len(session.messages) == 0

    session.add_user_message("hello again")
    # pops user only when there is no assistant
    assert session.undo() is True
    assert len(session.messages) == 0


def test_chat_session_build_payload_messages_empty():
    session = ChatSession(system_prompt="Sys")
    counter = MagicMock()
    session.set_counter(counter)

    assert session.build_payload_messages() == [{"role": "system", "content": "Sys"}]


def test_chat_session_build_payload_messages_busts_budget():
    session = ChatSession(system_prompt="Sys", ctx_size=2000, genmax=1000)
    counter = MagicMock()
    # system + user message consumes 1010 tokens, budget is 2000 - 1000 = 1000
    counter.count_messages.return_value = 1010
    session.set_counter(counter)
    session.add_user_message("Hello")

    assert session.build_payload_messages() is None


def test_chat_session_build_payload_messages_clipping():
    session = ChatSession(system_prompt="Sys", ctx_size=2000, genmax=1000)
    counter = TokenCounter(base_url="")
    counter._use_server = False  # use tiktoken
    session.set_counter(counter)

    # 2000 - 1000 = 1000 tokens budget.
    # We add 6 exchanges. tiktoken count for each simple exchange is small.
    # Let's mock counter.count_messages to emulate a budget bust on long context.
    original_count = counter.count_messages

    def mock_count_messages(messages):
        # Let's say if the length of messages is greater than 3, it busts the budget
        if len(messages) > 3:
            return 1500
        return original_count(messages)

    session._counter.count_messages = mock_count_messages

    # messages inside ChatSession:
    # 0. User: Msg 1
    # 1. Assistant: Reply 1
    # 2. User: Msg 2
    # 3. Assistant: Reply 2
    # 4. User: Msg 3 (latest user message)
    session.add_user_message("Msg 1")
    session.add_assistant_message("Reply 1")
    session.add_user_message("Msg 2")
    session.add_assistant_message("Reply 2")
    session.add_user_message("Msg 3")

    # Candidate message list would be:
    # [{"role": "system", "content": "Sys"},
    #  {"role": "user", "content": "Msg 1"},
    #  {"role": "assistant", "content": "Reply 1"},
    #  {"role": "user", "content": "Msg 2"},
    #  {"role": "assistant", "content": "Reply 2"},
    #  {"role": "user", "content": "Msg 3"}]
    #
    # Since len(messages) > 3, it keeps dropping the oldest messages from history.
    # First candidates dropped:
    # - Drops "Msg 1" (role system, user Reply 1, Msg 2, Reply 2, Msg 3).
    #   First non-system message is "Reply 1" (assistant), so it drops "Reply 1" as well.
    # - Candidate becomes: system, Msg 2, Reply 2, Msg 3 (length 4). Still len > 3.
    # - Drops Msg 2. First non-system is Reply 2 (assistant), so it drops Reply 2.
    # - Candidate becomes: system, Msg 3 (length 2).
    #   Length is <= 3, so it fits!
    payload = session.build_payload_messages()
    assert payload == [
        {"role": "system", "content": "Sys"},
        {"role": "user", "content": "Msg 3"},
    ]
