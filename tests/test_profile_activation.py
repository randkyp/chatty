"""Profile discovery must not depend on how a profile or model was selected."""

import copy

import pytest
from fastapi.testclient import TestClient

from chatty import main
from chatty.api import fetch_model_metadata
from chatty.chat_session import ChatSession
from chatty.commands import CommandResult, handle_command, sync_token_counter
from chatty.config import AppConfig, Profile, load_config, resolve_limits
from chatty.web import server


@pytest.fixture
def catalog(respx_mock):
    return respx_mock.get("http://up/v1/models").respond(
        200,
        json={
            "data": [
                {"id": "first", "context_length": 4096, "max_tokens": 512},
                {"id": "second", "context_length": 32768, "max_tokens": 2048},
            ]
        },
    )


@pytest.mark.parametrize("available", [True, False])
def test_switch_matches_startup(tmp_path, respx_mock, available):
    path = tmp_path / "config.toml"
    path.write_text('[profile.new]\nbase_url = "http://up"\n')
    respx_mock.get("http://up/v1/models").respond(
        200 if available else 503,
        json={"data": [{"id": "found", "context_length": 16384, "max_tokens": 1024}]},
    )
    startup = load_config(["--config", str(path), "--profile", "new"])
    resolve_limits(startup)
    cfg = AppConfig(path, Profile("old", "http://old", ctx_size=12345, genmax=678))
    session = ChatSession(ctx_size=12345, genmax=678, messages=[{"role": "user", "content": "keep"}])
    result = handle_command("/profile new", session, cfg)
    assert "Switched" in result.message
    assert (session.ctx_size, session.genmax) == (startup.ctx_size, startup.genmax)
    assert cfg.profile.model == startup.profile.model
    assert session.messages == [{"role": "user", "content": "keep"}]
    assert session._counter.base_url == "http://up"


@pytest.mark.parametrize("selection", ["startup", "command", "cli_picker", "web_picker"])
def test_selected_model_limits(tmp_path, catalog, monkeypatch, selection):
    cfg = AppConfig(tmp_path / "config.toml", Profile("p", "http://up"))
    session = ChatSession()
    if selection == "startup":
        cfg.profile.model = "second"
        resolve_limits(cfg)
        session = server._build_session(cfg)
    else:
        resolve_limits(cfg)
        session = server._build_session(cfg)
        assert session.ctx_size == 4096
        if selection == "command":
            handle_command("/models second", session, cfg)
        elif selection == "cli_picker":
            monkeypatch.setattr("chatty.ui.pick_model", lambda *_: "second")
            main._handle_command_result(CommandResult(model_choices=["first", "second"]), session, cfg)
        else:
            server.app.state.cfg = cfg
            with TestClient(server.app).websocket_connect("/ws") as ws:
                ws.receive_json()
                ws.send_json({"type": "model_select", "model": "second"})
                assert ws.receive_json()["type"] == "model_selected"
                state = server.app.state.current_web_session
                cfg, session = state.cfg, state.chat
    assert cfg.profile.model == "second"
    assert (session.ctx_size, session.genmax) == (32768, 2048)
    assert cfg.profile.ctx_size is None
    assert cfg.profile.genmax is None


def test_explicit_limits_survive_selection_and_inferred_limits_refresh(tmp_path, catalog):
    path = tmp_path / "config.toml"
    path.write_text('[profile.p]\nbase_url = "http://up"\nctx_size = 12345\n')
    cfg = load_config(["--config", str(path), "--profile", "p"])
    resolve_limits(cfg)
    session = server._build_session(cfg)
    handle_command("/models second", session, cfg)
    assert (session.ctx_size, session.genmax) == (12345, 2048)
    handle_command("/genmax 0", session, cfg)
    handle_command("/models first", session, cfg)
    assert (session.ctx_size, session.genmax) == (12345, 0)
    assert catalog.call_count == 2  # Explicit zero is an override, not missing.


def test_inferred_limits_are_not_saved(tmp_path, catalog):
    path = tmp_path / "config.toml"
    path.write_text('[profile.p]\nbase_url = "http://up"\n')
    cfg = load_config(["--config", str(path), "--profile", "p"])
    resolve_limits(cfg)
    result = handle_command("/samplers save", server._build_session(cfg), cfg)
    assert "saved" in result.message
    saved = load_config(["--config", str(path), "--profile", "p"])
    assert saved.profile.ctx_size is None
    assert saved.profile.genmax is None


def test_unknown_model_does_not_borrow_first_models_limits(tmp_path, catalog):
    cfg = AppConfig(tmp_path / "config.toml", Profile("p", "http://up"))
    resolve_limits(cfg)
    session = server._build_session(cfg)
    handle_command("/models unknown", session, cfg)
    assert cfg.profile.model == "unknown"
    assert (session.ctx_size, session.genmax) == (8192, 0)


@pytest.mark.parametrize("client", ["cli", "web"])
def test_key_entry_resolves_deferred_discovery(tmp_path, catalog, monkeypatch, client):
    cfg = AppConfig(tmp_path / "config.toml", Profile("p", "http://up", api_key_mode="ephemeral"))
    resolve_limits(cfg)
    assert not catalog.called
    session = server._build_session(cfg)
    if client == "cli":
        monkeypatch.setattr(main, "get_api_key_input", lambda: "secret")
        main._handle_command_result(CommandResult(request_api_key=True), session, cfg)
    else:
        server.app.state.cfg = cfg
        with TestClient(server.app).websocket_connect("/ws") as ws:
            ws.receive_json()
            assert ws.receive_json()["type"] == "api_key_prompt"
            ws.send_json({"type": "api_key", "api_key": "secret"})
            assert "set" in ws.receive_json()["content"]
            state = server.app.state.current_web_session
            cfg, session = state.cfg, state.chat
    assert cfg.profile.model == "first"
    assert (session.ctx_size, session.genmax) == (4096, 512)
    assert catalog.calls.last.request.headers["Authorization"] == "Bearer secret"


def test_shared_key_completes_discovery_in_other_session(tmp_path, catalog):
    cfg = AppConfig(tmp_path / "config.toml", Profile("p", "http://up", api_key_mode="ephemeral"))
    resolve_limits(cfg)
    other = copy.deepcopy(cfg)
    session = server._build_session(other)
    cfg.set_ephemeral_api_key("shared")
    sync_token_counter(session, other)
    assert other.profile.model == "first"
    assert (session.ctx_size, session.genmax) == (4096, 512)
    sync_token_counter(session, other)
    assert catalog.call_count == 1


@pytest.mark.parametrize("data", [None, [], {"data": None}, {"data": [None]}])
def test_malformed_catalog_falls_back(respx_mock, data):
    respx_mock.get("http://up/v1/models").respond(200, json=data)
    assert fetch_model_metadata("http://up", None, "second") == {}
