import chatty.main as main
from chatty.chat_session import ChatSession


def test_last_assistant_text():
    s = ChatSession()
    s.add_user_message("q")
    s.add_assistant_message("the answer")
    assert main._last_assistant_text(s) == "the answer"


def test_last_assistant_text_none_when_empty():
    assert main._last_assistant_text(ChatSession()) is None


def test_report_tokens_prefers_real_usage(monkeypatch):
    msgs = []
    monkeypatch.setattr(main, "print_system", lambda m: msgs.append(m))
    s = ChatSession()
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    main._report_tokens(s, usage, elapsed=2.0)
    assert "15" in msgs[0]
    assert "tok/s" in msgs[0]  # rate shown when elapsed > 0


def test_report_tokens_falls_back_to_estimate(monkeypatch):
    msgs = []
    monkeypatch.setattr(main, "print_system", lambda m: msgs.append(m))

    class _C:
        def count_messages(self, m):
            return 7

    s = ChatSession()
    s.set_counter(_C())
    main._report_tokens(s, usage=None, elapsed=1.0)
    assert "~7" in msgs[0]
    assert "budget" in msgs[0]


def test_stream_and_render_collects_text(monkeypatch):
    # Mock the API stream and the rich console interactions.
    def fake_stream(**kwargs):
        yield ("delta", "Hello ")
        yield ("delta", "world")
        yield ("usage", '{"completion_tokens": 2}')

    monkeypatch.setattr(main, "stream_chat", lambda **kw: fake_stream(**kw))

    from chatty.config import AppConfig, Profile

    cfg = AppConfig(config_path=None, profile=Profile(name="p", base_url="http://x"), raw_output=True)
    text, interrupted, usage = main.stream_and_render(cfg, [{"role": "user", "content": "hi"}], genmax=0)
    assert text == "Hello world"
    assert interrupted is False
    assert usage == {"completion_tokens": 2}
