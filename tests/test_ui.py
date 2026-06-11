from prompt_toolkit.document import Document

from chatty.config import AppConfig, Profile
from chatty.ui import ChattyCompleter, _build_key_bindings, create_prompt_session


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


def test_completer_path_does_not_crash(tmp_path):
    completer = ChattyCompleter(_cfg(tmp_path))
    # Should delegate to PathCompleter without raising.
    _completions(completer, "look at @/tmp/")


def test_build_key_bindings_modes():
    assert _build_key_bindings(enter_sends=True) is not None
    assert _build_key_bindings(enter_sends=False) is not None


def test_create_prompt_session():
    assert create_prompt_session(enter_sends=True) is not None
