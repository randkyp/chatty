"""Reasoning capture and replay: accumulator shapes, storage, and the flag."""

import pytest

from chatty.api import ReasoningAccumulator, stream_chat
from chatty.chat_session import ChatSession, TokenCounter
from chatty.commands import _read_session, save_session
from chatty.config import AppConfig, Profile, load_config, save_profile

# ── Accumulator ────────────────────────────────────────────────────────────


def test_accumulator_keeps_plain_reasoning_under_its_own_key():
    acc = ReasoningAccumulator()
    acc.feed({"reasoning": "hm"})
    acc.feed({"reasoning": "m"})

    assert acc.as_fields() == {"reasoning": "hmm"}


def test_accumulator_prefers_reasoning_content():
    acc = ReasoningAccumulator()
    acc.feed({"reasoning_content": "a"})
    acc.feed({"reasoning_content": "b"})

    assert acc.as_fields() == {"reasoning_content": "ab"}


def test_accumulator_merges_streamed_reasoning_details_by_index():
    acc = ReasoningAccumulator()
    acc.feed({"reasoning_details": [{"type": "reasoning.text", "index": 0, "text": "Let me "}]})
    acc.feed({"reasoning_details": [{"type": "reasoning.text", "index": 0, "text": "think"}]})
    acc.feed(
        {
            "reasoning_details": [
                {"type": "reasoning.text", "index": 0, "signature": "sig-1", "format": "anthropic-claude-v1"}
            ]
        }
    )
    acc.feed({"reasoning_details": [{"type": "reasoning.encrypted", "index": 1, "data": "AAAA"}]})

    assert acc.as_fields() == {
        "reasoning_details": [
            {
                "type": "reasoning.text",
                "index": 0,
                "text": "Let me think",
                "signature": "sig-1",
                "format": "anthropic-claude-v1",
            },
            {"type": "reasoning.encrypted", "index": 1, "data": "AAAA"},
        ]
    }


def test_accumulator_is_empty_without_reasoning():
    acc = ReasoningAccumulator()
    acc.feed({"content": "just an answer"})

    assert acc.as_fields() == {}
    assert acc.feed({"content": "x"}) is None


def test_stream_chat_fills_the_accumulator(respx_mock):
    body = (
        'data: {"choices": [{"delta": {"reasoning_content": "step "}}]}\n\n'
        'data: {"choices": [{"delta": {"reasoning_content": "one"}}]}\n\n'
        'data: {"choices": [{"delta": {"content": "answer"}}]}\n\n'
        "data: [DONE]\n"
    )
    respx_mock.post("http://up/v1/chat/completions").respond(
        200, text=body, headers={"content-type": "text/event-stream"}
    )

    acc = ReasoningAccumulator()
    events = list(stream_chat("http://up", None, "m", [{"role": "user", "content": "x"}], {}, 0, reasoning=acc))

    assert ("reasoning", "step ") in events
    assert acc.as_fields() == {"reasoning_content": "step one"}


def test_cli_records_reasoning_on_the_assistant_turn(respx_mock, tmp_path):
    from chatty import main

    body = (
        'data: {"choices": [{"delta": {"reasoning_content": "why"}}]}\n\n'
        'data: {"choices": [{"delta": {"content": "a"}}]}\n\n'
        "data: [DONE]\n"
    )
    respx_mock.post("http://up/v1/chat/completions").respond(
        200, text=body, headers={"content-type": "text/event-stream"}
    )
    cfg = AppConfig(
        config_path=tmp_path / "c.toml",
        profile=Profile(name="p", base_url="http://up", model="m"),
        raw_output=True,
    )
    session = _session()
    session.add_user_message("q")

    main._stream_record_report(session, cfg, session.build_payload_messages())

    assert session.messages[-1] == {
        "role": "assistant",
        "content": "a",
        "_reasoning": {"reasoning_content": "why"},
    }


# ── Storage and replay ─────────────────────────────────────────────────────


def _session() -> ChatSession:
    session = ChatSession(system_prompt="sys")
    counter = TokenCounter(base_url="")
    counter._use_server = False  # force tiktoken
    session.set_counter(counter)
    return session


def test_reasoning_is_stored_privately_and_replayed():
    session = _session()
    session.add_user_message("q")
    session.add_assistant_message("a", reasoning={"reasoning_content": "why"})
    session.add_user_message("q2")

    payload = session.build_payload_messages()

    assert payload[2] == {"role": "assistant", "content": "a", "reasoning_content": "why"}
    # The stored turn keeps reasoning under a private key, so display paths and
    # /copy only ever see the answer.
    assert session.messages[1]["content"] == "a"
    assert session.messages[1]["_reasoning"] == {"reasoning_content": "why"}


def test_reasoning_is_not_replayed_when_disabled():
    session = _session()
    session.preserve_reasoning = False
    session.add_user_message("q")
    session.add_assistant_message("a", reasoning={"reasoning_content": "why"})
    session.add_user_message("q2")

    payload = session.build_payload_messages()

    assert payload[2] == {"role": "assistant", "content": "a"}


def test_reasoning_cannot_inject_message_fields():
    session = _session()
    session.add_user_message("q")
    session.add_assistant_message("a", reasoning={"role": "system", "reasoning_content": "why"})
    session.add_user_message("q2")

    payload = session.build_payload_messages()

    assert payload[2]["role"] == "assistant"
    assert payload[2]["reasoning_content"] == "why"


def test_replayed_reasoning_counts_toward_the_budget():
    counter = TokenCounter(base_url="")
    counter._use_server = False

    plain = counter.count_messages([{"role": "assistant", "content": "a"}])
    with_reasoning = counter.count_messages([{"role": "assistant", "content": "a", "reasoning_content": "x " * 100}])

    assert with_reasoning > plain + 50


def test_opaque_blobs_do_not_inflate_the_count():
    counter = TokenCounter(base_url="")
    counter._use_server = False
    plain = counter.count_messages([{"role": "assistant", "content": "a"}])
    signed = counter.count_messages(
        [
            {
                "role": "assistant",
                "content": "a",
                "reasoning_details": [{"type": "reasoning.encrypted", "data": "A" * 4000}],
            }
        ]
    )

    assert signed == plain


def test_display_messages_hide_reasoning():
    session = _session()
    session.add_user_message("q")
    session.add_assistant_message("a", reasoning={"reasoning_content": "why"})

    assert session.display_messages() == [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]


def test_session_round_trip_keeps_reasoning(tmp_path):
    session = _session()
    session.add_user_message("q")
    session.add_assistant_message("a", reasoning={"reasoning_content": "why"})
    path = tmp_path / "s.json"

    save_session(session, path)
    messages, _metadata = _read_session(path)

    assert messages[1]["_reasoning"] == {"reasoning_content": "why"}


# ── Config ─────────────────────────────────────────────────────────────────


def test_preserve_reasoning_defaults_on(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[profile.default]\nbase_url = "http://x"\n')

    assert load_config(["-c", str(path)]).profile.preserve_reasoning is True


def test_preserve_reasoning_rejects_non_boolean(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[profile.default]\nbase_url = "http://x"\npreserve_reasoning = "yes"\n')

    with pytest.raises(Exception, match="preserve_reasoning"):
        load_config(["-c", str(path)])


def test_preserve_reasoning_round_trips(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[profile.default]\nbase_url = "http://x"\n')
    cfg = AppConfig(config_path=path, profile=Profile(name="default", base_url="http://x"))

    cfg.profile.preserve_reasoning = False
    save_profile(cfg)
    assert load_config(["-c", str(path)]).profile.preserve_reasoning is False

    cfg.profile.preserve_reasoning = True
    save_profile(cfg)
    assert "preserve_reasoning" not in path.read_text()
    assert load_config(["-c", str(path)]).profile.preserve_reasoning is True
