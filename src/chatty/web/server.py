"""
Web UI server: a FastAPI + WebSocket front-end that reuses the CLI's session,
command, and API machinery.

Run via ``chatty --web`` (see chatty.main) or ``python -m chatty.web.server``.
Config is loaded once at startup and shared across connections. Browser
sessions retain their own ChatSession and mutable config across reconnects.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from chatty.api import stream_chat_async
from chatty.chat_session import ChatSession
from chatty.commands import ClientType, handle_command, is_command, save_session, sync_token_counter
from chatty.config import EPHEMERAL_KEY_REQUIRED, AppConfig, load_config, resolve_limits

app = FastAPI()
BASE_DIR = Path(__file__).parent
PUBLIC_DIR = BASE_DIR / "public"

app.mount("/static", StaticFiles(directory=PUBLIC_DIR), name="static")


@app.get("/")
async def get_index() -> HTMLResponse:
    return HTMLResponse(content=(PUBLIC_DIR / "index.html").read_text(encoding="utf-8"))


def _build_session(cfg: AppConfig) -> ChatSession:
    session = ChatSession(
        system_prompt=cfg.profile.system_prompt,
        ctx_size=cfg.profile.ctx_size if cfg.profile.ctx_size is not None else 8192,
        genmax=cfg.profile.genmax if cfg.profile.genmax is not None else 0,
    )
    sync_token_counter(session, cfg)
    return session


def _welcome_text(cfg: AppConfig, session: ChatSession) -> str:
    gen_display = "unlimited" if session.genmax == 0 else session.genmax
    model = cfg.profile.model or "(auto)"
    return f"Welcome to chatty ({cfg.profile.name} - {model}). Context: {session.ctx_size}, Gen: {gen_display}."


@dataclass
class _WebSession:
    session_id: str
    client_id: str
    cfg: AppConfig
    chat: ChatSession
    connection: object | None = None


_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_AUTOSAVE_LOCK = threading.Lock()


def _valid_id(value: str | None) -> str | None:
    return value if value and _ID_RE.fullmatch(value) else None


def _acquire_web_session(
    base_cfg: AppConfig, session_id: str | None, client_id: str | None
) -> tuple[_WebSession, bool]:
    """Restore a browser's state, while keeping simultaneous browser sessions separate."""
    store_cfg = getattr(app.state, "web_sessions_cfg", None)
    if store_cfg is not base_cfg:
        app.state.web_sessions = {}
        app.state.web_sessions_cfg = base_cfg

    store: dict[str, _WebSession] = app.state.web_sessions
    requested_id = _valid_id(session_id)
    browser_id = _valid_id(client_id) or secrets.token_urlsafe(24)
    state = store.get(requested_id) if requested_id else None
    restored = state is not None

    # A duplicated tab can inherit sessionStorage. Fork it rather than letting
    # two live pages mutate one conversation. A reconnect from the same page
    # keeps its client_id and may replace a not-yet-cleaned-up socket.
    if state is not None and state.connection is not None and state.client_id != browser_id:
        state = None
        restored = False

    if state is None:
        new_id = requested_id if requested_id not in store else secrets.token_urlsafe(24)
        state_cfg = copy.deepcopy(base_cfg)
        state = _WebSession(new_id or secrets.token_urlsafe(24), browser_id, state_cfg, _build_session(state_cfg))
        store[state.session_id] = state
    else:
        state.client_id = browser_id

    state.connection = object()
    app.state.current_web_session = state
    return state, restored


def _save_autosave(session: ChatSession) -> Path:
    path = Path.home() / ".config" / "chatty" / "session.json"
    # Multiple isolated browser sessions may disconnect together. Serialize
    # their writes so session.json is always a complete session.
    with _AUTOSAVE_LOCK:
        save_session(session, path)
    return path


def _autosave_web_session(cfg: AppConfig) -> Path | None:
    state: _WebSession | None = getattr(app.state, "current_web_session", None)
    if not cfg.autosave or state is None or not state.chat.messages:
        return None
    return _save_autosave(state.chat)


class _Sender:
    """Send JSON messages from the connection's event loop."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    async def send(self, msg_type: str, content: object, **extra) -> None:
        payload = {"type": msg_type, "content": content, **extra}
        try:
            await self._ws.send_text(json.dumps(payload))
        except RuntimeError:
            pass


def _coerce_images(raw_images: object) -> list[dict]:
    """Turn browser-supplied image payloads into the internal image dict shape."""
    images: list[dict] = []
    if isinstance(raw_images, list):
        for item in raw_images:
            if isinstance(item, dict) and item.get("data_url"):
                images.append(
                    {
                        "data_url": item["data_url"],
                        "path": None,
                        "mime_type": item.get("mime_type", "image/png"),
                    }
                )
    return images


async def _stream_to_ws(
    cfg: AppConfig,
    session: ChatSession,
    messages: list[dict],
    sender: _Sender,
    streaming: asyncio.Event,
    *,
    record: bool,
) -> None:
    if cfg.ephemeral_api_key_missing:
        if record:
            session.revert_last_user_message()
        await sender.send("error", EPHEMERAL_KEY_REQUIRED)
        return

    await sender.send("stream_start", "", ephemeral=not record)
    collected: list[str] = []
    streaming.set()
    try:
        async for event_type, content in stream_chat_async(
            base_url=cfg.profile.base_url,
            api_key=cfg.effective_api_key,
            model=cfg.profile.model or "default",
            messages=messages,
            samplers=cfg.profile.samplers,
            genmax=session.genmax,
            debug=cfg.debug,
        ):
            if event_type == "error":
                await sender.send("error", content)
                continue
            if event_type == "delta":
                collected.append(content)
                await sender.send("stream_chunk", content)
    except asyncio.CancelledError:
        pass
    finally:
        streaming.clear()
    full_response = "".join(collected)
    if record:
        if full_response:
            session.add_assistant_message(full_response)
        else:
            session.revert_last_user_message()
    await sender.send("stream_end", "")


async def _handle_message(
    text: str,
    raw_images: object,
    session: ChatSession,
    cfg: AppConfig,
    sender: _Sender,
    streaming: asyncio.Event,
) -> bool:
    """Handle one user message. Returns True if the client asked to quit."""
    # ── Slash commands ──────────────────────────────────────────────────────
    if is_command(text):
        first_word = text.split(None, 1)[0]
        if first_word == "/theme":
            await sender.send("theme", text)  # handled client-side
            return False

        await sender.send("command_start", first_word)
        try:
            result = await asyncio.to_thread(handle_command, text, session, cfg, client_type=ClientType.WEB)
        except Exception as e:  # noqa: BLE001 - surface command failures to the user
            await sender.send("command_end", "")
            await sender.send("error", f"Command Error: {e}")
            return False
        await sender.send("command_end", "")

        if result.request_api_key:
            if result.message:
                await sender.send("system", result.message)
            await sender.send("api_key_prompt", "")
            return False

        if result.quit:
            await sender.send("system", "Goodbye!")
            return True

        if (
            result.clear_dom
            or result.remove_last_exchange
            or result.remove_last_assistant
            or result.load_messages is not None
        ):
            await sender.send("history", session.messages)

        if result.ephemeral_prompt:
            messages = [{"role": "user", "content": result.ephemeral_prompt}]
            await _stream_to_ws(cfg, session, messages, sender, streaming, record=False)
            return False

        if result.resend_user:
            payload = session.build_payload_messages()
            if payload is None:
                await sender.send("warning", "Context budget exceeded.")
            else:
                await _stream_to_ws(cfg, session, payload, sender, streaming, record=True)
            return False

        if result.copy_last:
            last = next(
                (m.get("content") for m in reversed(session.messages) if m.get("role") == "assistant"),
                None,
            )
            if isinstance(last, str) and last:
                await sender.send("copy_to_clipboard", last)
            else:
                await sender.send("system", "No assistant response to copy.")
            return False

        if result.model_choices is not None:
            if result.model_choices:
                await sender.send("model_picker", result.model_choices, current=result.model_current or "")
            elif result.message:
                await sender.send("system", result.message)
            return False

        if result.message:
            await sender.send("system", result.message)
        return False

    # ── Escape // → literal / ───────────────────────────────────────────────
    if text.startswith("//"):
        text = text[1:]

    # ── Normal send ─────────────────────────────────────────────────────────
    payload_messages, attached = session.stage_user_message(text, extra_images=_coerce_images(raw_images))
    for img in attached:
        label = img["path"] if img.get("path") else "clipboard/upload"
        await sender.send("system", f" -> Attached image: {label}")

    if payload_messages is None:
        reserve = session.genmax if session.genmax > 0 else (session.ctx_size // 10)
        await sender.send(
            "warning",
            f"Message + system prompt exceeds context budget "
            f"({session.ctx_size} - {reserve} = {session.context_budget} tokens). "
            "Try /clear or /ctx to increase the limit.",
        )
        session.revert_last_user_message()
        return False

    await _stream_to_ws(cfg, session, payload_messages, sender, streaming, record=True)
    return False


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()

    base_cfg: AppConfig = getattr(app.state, "cfg", None) or load_config([])
    state, restored = _acquire_web_session(
        base_cfg,
        websocket.query_params.get("session_id"),
        websocket.query_params.get("client_id"),
    )
    cfg = state.cfg
    session = state.chat
    connection = state.connection

    sender = _Sender(websocket)
    await sender.send(
        "welcome",
        _welcome_text(cfg, session),
        session_id=state.session_id,
        restored=restored,
        history=session.messages,
    )
    if cfg.ephemeral_api_key_missing:
        await sender.send("api_key_prompt", "")

    current_task: asyncio.Task | None = None
    streaming = asyncio.Event()

    try:
        while True:
            data = await websocket.receive_text()
            try:
                req = json.loads(data)
            except json.JSONDecodeError:
                continue

            msg_type = req.get("type")
            app.state.current_web_session = state

            if msg_type == "cancel":
                if streaming.is_set() and current_task is not None:
                    current_task.cancel()
                    await current_task
                continue

            if msg_type == "api_key":
                if cfg.profile.api_key_mode != "ephemeral":
                    await sender.send(
                        "error",
                        'API key entry is only available for profiles using api_key_mode = "ephemeral".',
                    )
                    continue
                api_key = req.get("api_key")
                if not isinstance(api_key, str) or not api_key.strip():
                    await sender.send("system", "API key unchanged.")
                    continue
                cfg.set_ephemeral_api_key(api_key)
                sync_token_counter(session, cfg)
                await sender.send("system", f"Ephemeral API key set for profile '{cfg.profile.name}'.")
                continue

            if msg_type == "model_select":
                from chatty.commands import apply_model_choice

                model = req.get("model")
                if not isinstance(model, str) or not model:
                    continue
                confirmation = apply_model_choice(cfg, model)
                await sender.send("model_selected", confirmation)
                continue

            if msg_type != "message":
                continue

            # Another connection may have changed this profile's shared key.
            sync_token_counter(session, cfg)

            text = (req.get("text") or "").strip()
            if not text and not req.get("images"):
                continue

            if current_task is not None and not current_task.done():
                await sender.send("submission_rejected", "A response is still generating; press Stop first.")
                continue

            async def _runner(text=text, images=req.get("images")) -> None:
                before = copy.deepcopy(session.messages)
                command = is_command(text)
                accepted = False
                try:
                    quit_requested = await _handle_message(text, images, session, cfg, sender, streaming)
                    accepted = command or (
                        session.messages != before
                        and bool(session.messages)
                        and session.messages[-1].get("role") == "assistant"
                    )
                    if quit_requested:
                        await websocket.close()
                except Exception as e:  # noqa: BLE001
                    # stage_user_message rolls back preparation failures. Only
                    # revert here when a later request step mutated history.
                    if not command and session.messages != before:
                        session.revert_last_user_message()
                    await sender.send("error", f"Request failed: {e}")
                finally:
                    # ponytail: full snapshots cost O(history size), including images;
                    # use stable turn IDs and deltas if long sessions make this costly.
                    await sender.send("submission_done", session.messages, accepted=accepted)

            current_task = asyncio.create_task(_runner())
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"WebSocket Error: {e}")
    finally:
        if streaming.is_set() and current_task is not None:
            current_task.cancel()
            await current_task
        if state.connection is connection:
            state.connection = None
            if cfg.autosave and session.messages:
                try:
                    await asyncio.to_thread(_save_autosave, session)
                except OSError as e:
                    print(f"Autosave failed: {e}")


def run_server(argv: list[str] | None = None, *, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Load config once, share it across connections, and serve the web UI."""
    import uvicorn

    cfg = load_config(argv)
    resolve_limits(cfg)
    app.state.cfg = cfg
    app.state.web_sessions = {}
    app.state.web_sessions_cfg = cfg
    app.state.current_web_session = None
    try:
        uvicorn.run(app, host=host, port=port)
    finally:
        try:
            _autosave_web_session(cfg)
        except OSError as e:
            print(f"Autosave failed: {e}")


if __name__ == "__main__":
    run_server()
