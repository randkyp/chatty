import asyncio
import json
import re
import sys
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from chatty.api import stream_chat
from chatty.chat_session import ChatSession, TokenCounter
from chatty.commands import handle_command

# Import existing logic from chatty
from chatty.config import load_config
from chatty.images import extract_images_from_text
from chatty.main import _resolve_limits

app = FastAPI()
BASE_DIR = Path(__file__).parent
PUBLIC_DIR = BASE_DIR / "public"

PUBLIC_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=PUBLIC_DIR), name="static")


@app.get("/")
async def get_index():
    with open(PUBLIC_DIR / "index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    cfg = load_config(sys.argv[1:])
    _resolve_limits(cfg)

    session = ChatSession(
        system_prompt=cfg.profile.system_prompt,
        ctx_size=cfg.profile.ctx_size if cfg.profile.ctx_size is not None else 8192,
        genmax=cfg.profile.genmax if cfg.profile.genmax is not None else 0,
    )
    counter = TokenCounter(base_url=cfg.profile.base_url)
    session.set_counter(counter)

    async def send_msg(msg_type, content, **kwargs):
        payload = {"type": msg_type, "content": content}
        payload.update(kwargs)
        try:
            await websocket.send_text(json.dumps(payload))
        except RuntimeError:
            pass

    gen_display = "unlimited" if session.genmax == 0 else session.genmax
    welcome_msg = (
        f"Welcome to chatty ({cfg.profile.name} - {cfg.profile.model}). "
        f"Context: {session.ctx_size}, Gen: {gen_display}."
    )
    await send_msg("welcome", welcome_msg)

    loop = asyncio.get_running_loop()

    try:
        while True:
            data = await websocket.receive_text()
            req = json.loads(data)

            if req.get("type") == "message":
                text = req.get("text", "").strip()
                if not text:
                    continue

                # Check for slash commands
                first_word = text.split(None, 1)[0] if text else ""
                if text.startswith("/") and not text.startswith("//") and re.match(r"^/[a-zA-Z]+$", first_word):
                    # Handle theme switching directly in JS but if it's sent to backend, ignore or handle
                    if first_word == "/theme":
                        await send_msg("theme", text)  # Broadcast back so JS can handle it
                        continue

                    # Commands modify state and can return quit/messages
                    await send_msg("command_start", first_word)
                    try:
                        result = await asyncio.to_thread(handle_command, text, session, cfg)
                    except Exception as e:
                        await send_msg("command_end", "")
                        await send_msg("error", f"Command Error: {e}")
                        continue

                    await send_msg("command_end", "")
                    if result.quit:
                        await send_msg("system", "Goodbye!")
                        await websocket.close()
                        break

                    if result.ephemeral_prompt:
                        payload_messages = [{"role": "user", "content": result.ephemeral_prompt}]
                        model = cfg.profile.model or "default"
                        await send_msg("stream_start", "")

                        def run_ephemeral_stream():
                            try:
                                stream = stream_chat(
                                    base_url=cfg.profile.base_url,
                                    api_key=cfg.profile.api_key,
                                    model=model,
                                    messages=payload_messages,
                                    samplers=cfg.profile.samplers,
                                    genmax=session.genmax,
                                    debug=cfg.debug,
                                )
                                for event_type, content in stream:
                                    if event_type == "error":
                                        asyncio.run_coroutine_threadsafe(send_msg("error", content), loop)
                                        break
                                    asyncio.run_coroutine_threadsafe(send_msg("stream_chunk", content), loop)

                                asyncio.run_coroutine_threadsafe(send_msg("stream_end", ""), loop)
                            except Exception as e:
                                asyncio.run_coroutine_threadsafe(send_msg("error", f"Unexpected Error: {e}"), loop)
                                asyncio.run_coroutine_threadsafe(send_msg("stream_end", ""), loop)

                        await asyncio.to_thread(run_ephemeral_stream)
                        continue

                    if result.copy_last:
                        last_msg = None
                        for msg in reversed(session.messages):
                            if msg.get("role") == "assistant":
                                last_msg = msg.get("content")
                                break
                        if last_msg:
                            await send_msg("copy_to_clipboard", last_msg)
                        else:
                            await send_msg("system", "No assistant response to copy.")
                        continue

                    if result.message:
                        await send_msg("system", result.message)
                    continue

                if text.startswith("//"):
                    text = text[1:]

                text, text_images = extract_images_from_text(text)
                all_images = session.pending_images + text_images

                for img in all_images:
                    await send_msg("system", f" -> Attached image: {img.get('path', 'clipboard')}")

                session.add_user_message(text, images=all_images)
                session.pending_images.clear()

                payload_messages = session.build_payload_messages()
                if payload_messages is None:
                    budget = session.ctx_size - session.genmax
                    msg = (
                        f"Message + system prompt exceeds context budget "
                        f"({session.ctx_size} - {session.genmax} = {budget} tokens). "
                        "Try /clear or /ctx to increase the limit."
                    )
                    await send_msg("warning", msg)
                    session.undo()
                    continue

                model = cfg.profile.model or "default"

                await send_msg("stream_start", "")

                def run_stream():
                    try:
                        stream = stream_chat(
                            base_url=cfg.profile.base_url,
                            api_key=cfg.profile.api_key,
                            model=model,
                            messages=payload_messages,
                            samplers=cfg.profile.samplers,
                            genmax=session.genmax,
                            debug=cfg.debug,
                        )
                        collected = []
                        for event_type, content in stream:
                            if event_type == "error":
                                asyncio.run_coroutine_threadsafe(send_msg("error", content), loop)
                                break
                            collected.append(content)
                            asyncio.run_coroutine_threadsafe(send_msg("stream_chunk", content), loop)

                        full_response = "".join(collected)
                        if full_response:
                            session.add_assistant_message(full_response)
                        else:
                            session.undo()

                        asyncio.run_coroutine_threadsafe(send_msg("stream_end", ""), loop)

                    except Exception as e:
                        asyncio.run_coroutine_threadsafe(send_msg("error", f"Unexpected Error: {e}"), loop)
                        session.undo()
                        asyncio.run_coroutine_threadsafe(send_msg("stream_end", ""), loop)

                # Run blocking API call in a thread
                await asyncio.to_thread(run_stream)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WebSocket Error: {e}")


if __name__ == "__main__":
    import uvicorn

    # Pass app directly because src-web is not a valid python package name
    uvicorn.run(app, host="127.0.0.1", port=8000)
