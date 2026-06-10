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


def test_chat_session_context_budget():
    session = ChatSession(ctx_size=1000, genmax=0)
    assert session.context_budget == 900

    session = ChatSession(ctx_size=1000, genmax=200)
    assert session.context_budget == 800


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


# --- Merge-aware revert (undo-eats-merged-messages bug) ---


def test_revert_preserves_merged_user_text():
    """revert_last_user_message must not discard earlier merged-in user text."""
    session = ChatSession()
    session.add_user_message("keep me")
    session.add_user_message("oops, failed send")  # merged into the previous user msg
    assert len(session.messages) == 1  # merged

    session.revert_last_user_message()
    assert len(session.messages) == 1
    assert session.messages[0]["content"] == "keep me"


def test_revert_pops_non_merged_user_message():
    session = ChatSession()
    session.add_assistant_message("a")  # leading assistant (edge case)
    session.add_user_message("hello")  # new message, not merged (prev is assistant)
    session.revert_last_user_message()
    assert len(session.messages) == 1
    assert session.messages[0]["role"] == "assistant"


def test_revert_is_idempotent_after_assistant_message():
    session = ChatSession()
    session.add_user_message("q")
    session.add_assistant_message("a")  # clears the revert token
    session.revert_last_user_message()  # no-op
    assert [m["role"] for m in session.messages] == ["user", "assistant"]


def test_append_returns_merge_info():
    session = ChatSession()
    assert session._append("user", "a") == (False, None)
    merged, original = session._append("user", "b")
    assert merged is True
    assert original == "a"


# --- stage_user_message ---


class _FakeCounter:
    def __init__(self, per_message=1):
        self.per_message = per_message

    def count(self, text):
        return len(text.split())

    def count_messages(self, messages):
        return self.per_message * len(messages)


def test_stage_user_message_returns_payload_and_attached():
    session = ChatSession(system_prompt="sys")
    session.set_counter(_FakeCounter())
    payload, attached = session.stage_user_message("hello world")
    assert attached == []
    assert payload[-1] == {"role": "user", "content": "hello world"}
    assert payload[0]["role"] == "system"


def test_stage_user_message_bust_returns_none_and_revert_restores():
    session = ChatSession()
    session.set_counter(_FakeCounter(per_message=10_000))  # always busts
    payload, _ = session.stage_user_message("too big")
    assert payload is None
    session.revert_last_user_message()
    assert session.messages == []


# --- Context-clipping edge cases ---


def test_build_payload_drops_leading_assistant():
    session = ChatSession(system_prompt="sys")
    session.set_counter(_FakeCounter())
    session.messages = [
        {"role": "assistant", "content": "stray"},
        {"role": "user", "content": "u"},
    ]
    payload = session.build_payload_messages()
    assert [m["role"] for m in payload] == ["system", "user"]


def test_count_messages_image_cost():
    counter = TokenCounter(base_url="")
    counter._use_server = False  # force tiktoken
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }
    ]
    # Each image contributes a ~1000-token buffer.
    assert counter.count_messages(msgs) >= 1000
