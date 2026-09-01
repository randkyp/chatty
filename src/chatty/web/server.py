"""
Web UI server: a FastAPI + WebSocket front-end that reuses the CLI's session,
command, and API machinery.

Run via ``chatty --web`` (see chatty.main) or ``python -m chatty.web.server``.
Config is loaded once at startup and shared across connections; each WebSocket
gets its own ChatSession (and a private copy of the config it may mutate via
slash commands).
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from chatty.api import stream_chat
from chatty.chat_session import ChatSession, TokenCounter
from chatty.commands import ClientType, handle_command, is_command
from chatty.config import AppConfig, load_config, resolve_limits

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
    session.set_counter(TokenCounter(base_url=cfg.profile.base_url))
    return session


def _welcome_text(cfg: AppConfig, session: ChatSession) -> str:
    gen_display = "unlimited" if session.genmax == 0 else session.genmax
    model = cfg.profile.model or "(auto)"
    return f"Welcome to chatty ({cfg.profile.name} - {model}). Context: {session.ctx_size}, Gen: {gen_display}."


class _Sender:
    """Sends JSON messages to a WebSocket, callable from worker threads too."""

    def __init__(self, ws: WebSocket, loop: asyncio.AbstractEventLoop) -> None:
        self._ws = ws
        self._loop = loop

    async def send(self, msg_type: str, content: str, **extra) -> None:
        payload = {"type": msg_type, "content": content, **extra}
        try:
            await self._ws.send_text(json.dumps(payload))
        except RuntimeError:
            pass

    def send_threadsafe(self, msg_type: str, content: str, **extra) -> None:
        """Schedule a send from a non-async worker thread."""
        asyncio.run_coroutine_threadsafe(self.send(msg_type, content, **extra), self._loop)


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


def _run_stream(
    cfg: AppConfig,
    session: ChatSession,
    messages: list[dict],
    sender: _Sender,
    cancel: threading.Event,
) -> str:
    """Blocking stream consumer (runs in a worker thread). Returns collected text."""
    collected: list[str] = []
    stream = stream_chat(
        base_url=cfg.profile.base_url,
        api_key=cfg.profile.api_key,
        model=cfg.profile.model or "default",
        messages=messages,
        samplers=cfg.profile.samplers,
        genmax=session.genmax,
        debug=cfg.debug,
    )
    try:
        for event_type, content in stream:
            if cancel.is_set():
                break
            if event_type == "error":
                sender.send_threadsafe("error", content)
                break
            if event_type == "usage":
                continue
            collected.append(content)
            sender.send_threadsafe("stream_chunk", content)
    finally:
        stream.close()
    return "".join(collected)


async def _stream_to_ws(
    cfg: AppConfig,
    session: ChatSession,
    messages: list[dict],
    sender: _Sender,
    cancel: threading.Event,
    *,
    record: bool,
) -> None:
    await sender.send("stream_start", "")
    full_response = await asyncio.to_thread(_run_stream, cfg, session, messages, sender, cancel)
    # Record outside the worker thread so a late error can never mis-undo a
    # completed exchange (the old code called undo() from the except block).
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
    cancel: threading.Event,
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

        if result.quit:
            await sender.send("system", "Goodbye!")
            return True

        if result.clear_dom:
            await sender.send("clear_dom", "")

        if result.remove_last_exchange:
            await sender.send("remove_last_exchange", "")

        if result.remove_last_assistant:
            await sender.send("remove_last_assistant", "")

        if result.load_messages is not None:
            await sender.send("load_history", result.load_messages[-50:])

        if result.ephemeral_prompt:
            messages = [{"role": "user", "content": result.ephemeral_prompt}]
            await _stream_to_ws(cfg, session, messages, sender, cancel, record=False)
            return False

        if result.resend_user:
            payload = session.build_payload_messages()
            if payload is None:
                await sender.send("warning", "Context budget exceeded.")
            else:
                await _stream_to_ws(cfg, session, payload, sender, cancel, record=True)
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

    await _stream_to_ws(cfg, session, payload_messages, sender, cancel, record=True)
    return False


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()

    # Reuse the config resolved once at startup; copy so per-connection slash
    # commands (e.g. /profile) don't bleed across tabs.
    base_cfg: AppConfig = getattr(app.state, "cfg", None) or load_config([])
    cfg = copy.deepcopy(base_cfg)
    session = _build_session(cfg)

    loop = asyncio.get_running_loop()
    sender = _Sender(websocket, loop)
    await sender.send("welcome", _welcome_text(cfg, session))

    current_task: asyncio.Task | None = None
    current_cancel: threading.Event | None = None

    try:
        while True:
            data = await websocket.receive_text()
            try:
                req = json.loads(data)
            except json.JSONDecodeError:
                continue

            msg_type = req.get("type")

            if msg_type == "cancel":
                if current_cancel is not None:
                    current_cancel.set()
                continue

            if msg_type != "message":
                continue

            text = (req.get("text") or "").strip()
            if not text and not req.get("images"):
                continue

            if current_task is not None and not current_task.done():
                await sender.send("warning", "A response is still generating; press Stop first.")
                continue

            current_cancel = threading.Event()

            async def _runner(text=text, images=req.get("images"), cancel=current_cancel) -> None:
                quit_requested = await _handle_message(text, images, session, cfg, sender, cancel)
                if quit_requested:
                    await websocket.close()

            current_task = asyncio.create_task(_runner())
    except WebSocketDisconnect:
        if current_cancel is not None:
            current_cancel.set()
    except Exception as e:  # noqa: BLE001
        print(f"WebSocket Error: {e}")


def run_server(argv: list[str] | None = None, *, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Load config once, share it across connections, and serve the web UI."""
    import uvicorn

    cfg = load_config(argv)
    resolve_limits(cfg)
    app.state.cfg = cfg
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run_server()
