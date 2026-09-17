from io import StringIO

from prompt_toolkit.data_structures import Size
from prompt_toolkit.document import Document
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

from chatty.config import AppConfig, Profile
from chatty.ui import ChattyCompleter, _build_key_bindings, create_prompt_session, model_picker_max_visible, pick_model


def _completions(completer, text):
    doc = Document(text, len(text))
    return [c.text for c in completer.get_completions(doc, None)]


def _cfg(tmp_path, body='[profile.work]\nbase_url = "http://w"\n[profile.home]\nbase_url = "http://h"\n'):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(body)
    return AppConfig(config_path=cfg_path, profile=Profile(name="work", base_url="http://w"))


def test_completer_commands(tmp_path):
    completer = ChattyCompleter(_cfg(tmp_path))
    out = _completions(completer, "/sa")
    assert "/save" in out
    assert "/samplers" in out
    assert "/help" not in out


def test_completer_profiles(tmp_path):
    completer = ChattyCompleter(_cfg(tmp_path))
    out = _completions(completer, "/profile h")
    assert out == ["home"]


def test_completer_models(tmp_path, monkeypatch):
    import chatty.api

    monkeypatch.setattr(chatty.api, "list_models", lambda b, a: ["gpt-4o", "gpt-4o-mini"])
    completer = ChattyCompleter(_cfg(tmp_path))
    out = _completions(completer, "/models gpt-4o-m")
    assert out == ["gpt-4o-mini"]


def test_completer_models_waits_for_ephemeral_key(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.profile.api_key_mode = "ephemeral"
    calls = []
    monkeypatch.setattr("chatty.api.list_models", lambda base_url, api_key: calls.append(api_key) or ["model"])
    completer = ChattyCompleter(cfg)

    assert _completions(completer, "/models m") == []
    assert calls == []

    cfg.set_ephemeral_api_key("runtime-secret")
    assert _completions(completer, "/models m") == ["model"]
    assert calls == ["runtime-secret"]


def test_completer_path_does_not_crash(tmp_path):
    completer = ChattyCompleter(_cfg(tmp_path))
    # Should delegate to PathCompleter without raising.
    _completions(completer, "look at @/tmp/")


def test_build_key_bindings_modes():
    assert _build_key_bindings(enter_sends=True) is not None
    assert _build_key_bindings(enter_sends=False) is not None


def test_create_prompt_session():
    assert create_prompt_session(enter_sends=True) is not None


def _run_picker(keys, models, current=None):
    rendered = StringIO()
    output = Vt100_Output(
        rendered,
        lambda: Size(rows=24, columns=80),
        term="xterm-256color",
        enable_cpr=False,
    )
    with create_pipe_input() as pipe_input:
        pipe_input.send_text(keys)
        selected = pick_model(models, current, _input=pipe_input, _output=output)
    return selected, rendered.getvalue()


def test_model_picker_selects_current_and_single_choice():
    assert _run_picker("\r", ["alpha", "beta"], current="beta")[0] == "beta"
    assert _run_picker("\r", ["only"])[0] == "only"


def test_model_picker_filters_and_only_arrows_move_highlight():
    assert _run_picker("gm\r", ["alpha", "gamma", "gpt-4-mini"])[0] == "gamma"
    assert _run_picker("\x1b[B\r", ["alpha", "beta", "gamma"])[0] == "beta"


def test_model_picker_enter_with_no_matches_does_nothing_then_cancel_is_silent():
    selected, _ = _run_picker("zzz\r\x03", ["alpha", "beta"])
    assert selected is None


def test_model_picker_preserves_scrollback_and_bounds_rows():
    _, rendered = _run_picker("\x03", [f"model-{i}" for i in range(12)])
    assert "\x1b[2J" not in rendered
    assert "\x1b[?1049h" not in rendered
    assert model_picker_max_visible(24) == 8
    assert model_picker_max_visible(8) == 3
    assert model_picker_max_visible(4) == 1
