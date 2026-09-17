import pytest

import chatty.main as main
from chatty.chat_session import ChatSession
from chatty.commands import CommandResult
from chatty.config import AppConfig, Profile


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


def test_format_elapsed_uses_compact_duration_units():
    assert main._format_elapsed(5.2) == "5s"
    assert main._format_elapsed(60.0) == "1m"
    assert main._format_elapsed(90.0) == "1m 30s"
    assert main._format_elapsed(3_600.0) == "1h"


def test_stream_and_render_collects_text(monkeypatch):
    # Mock the API stream and the rich console interactions.
    def fake_stream(**kwargs):
        yield ("delta", "Hello ")
        yield ("delta", "world")
        yield ("usage", '{"completion_tokens": 2}')

    monkeypatch.setattr(main, "stream_chat", lambda **kw: fake_stream(**kw))

    cfg = AppConfig(config_path=None, profile=Profile(name="p", base_url="http://x"), raw_output=True)
    text, interrupted, usage = main.stream_and_render(cfg, [{"role": "user", "content": "hi"}], genmax=0)
    assert text == "Hello world"
    assert interrupted is False
    assert usage == {"completion_tokens": 2}


def test_apikey_prompt_sets_key_without_provider_validation(monkeypatch):
    cfg = AppConfig(
        config_path=None,
        profile=Profile(name="private", base_url="http://up", api_key_mode="ephemeral"),
    )
    session = ChatSession()
    messages = []
    monkeypatch.setattr(main, "get_api_key_input", lambda: "new-secret")
    monkeypatch.setattr(main, "print_system", messages.append)
    monkeypatch.setattr(main, "stream_chat", lambda **_: (_ for _ in ()).throw(AssertionError("must not validate")))

    assert (
        main._handle_command_result(CommandResult(message="Switched profile.", request_api_key=True), session, cfg)
        is False
    )
    assert cfg.effective_api_key == "new-secret"
    assert session._counter.api_key == "new-secret"
    assert messages[0] == "Switched profile."
    assert "set" in messages[-1]


def test_apikey_prompt_cancel_preserves_existing_key(monkeypatch):
    cfg = AppConfig(
        config_path=None,
        profile=Profile(name="private", base_url="http://up", api_key_mode="ephemeral"),
    )
    cfg.set_ephemeral_api_key("existing")
    monkeypatch.setattr(main, "get_api_key_input", lambda: "")
    monkeypatch.setattr(main, "print_system", lambda _: None)

    main._handle_command_result(CommandResult(request_api_key=True), ChatSession(), cfg)
    assert cfg.effective_api_key == "existing"


def test_load_history_does_not_clear_terminal_scrollback(capsys):
    cfg = AppConfig(config_path=None, profile=Profile(name="p", base_url="http://x"))
    result = CommandResult(
        load_messages=[
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"},
        ]
    )

    main._handle_command_result(result, ChatSession(), cfg)

    output = capsys.readouterr().out
    assert "\033[H\033[J" not in output
    assert "earlier question" in output
    assert "earlier answer" in output
    assert "Session restored. 2 messages loaded (displaying last 2)." in output


def test_stream_is_blocked_locally_without_ephemeral_key(monkeypatch):
    cfg = AppConfig(
        config_path=None,
        profile=Profile(name="private", base_url="http://up", api_key_mode="ephemeral"),
    )
    errors = []
    monkeypatch.setattr(main, "print_error", errors.append)
    monkeypatch.setattr(main, "stream_chat", lambda **_: pytest.fail("unexpected upstream request"))

    assert main.stream_and_render(cfg, [], 0) == ("", False, None)
    assert "/apikey" in errors[0]


def test_main_preprompts_for_missing_ephemeral_key(monkeypatch):
    from types import SimpleNamespace

    cfg = AppConfig(
        config_path=None,
        profile=Profile(
            name="private",
            base_url="http://up",
            api_key_mode="ephemeral",
            model="m",
            ctx_size=4096,
            genmax=0,
        ),
    )
    prompts = []
    monkeypatch.setattr(main, "parse_args", lambda _: SimpleNamespace(web=False))
    monkeypatch.setattr(main, "load_config", lambda _: cfg)
    monkeypatch.setattr(main, "resolve_limits", lambda _: None)
    monkeypatch.setattr(main, "print_welcome", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "create_prompt_session", lambda **kwargs: object())
    monkeypatch.setattr(main, "get_user_input", lambda _: None)
    monkeypatch.setattr(main, "get_api_key_input", lambda: prompts.append("prompted") or "startup-secret")
    monkeypatch.setattr(main, "print_system", lambda _: None)
    monkeypatch.setattr(main, "close_clients", lambda: None)

    main.main([])

    assert prompts == ["prompted"]
    assert cfg.effective_api_key == "startup-secret"
