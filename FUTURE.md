# FUTURE.md — Improvement Avenues

An evaluation of the current state of the codebase (branch `feature/web-app`, June 2026)
with concrete avenues for improvement. Findings are grouped by theme and roughly
prioritized within each section.

> **Progress tracking.** Items are checked off (`[x]`) as they are implemented.
> Decisions locked for this pass: full packaging restructure (`chatty/web/` + `chatty --web`),
> keep `pyperclip` and delete the manual `/copy` fallback, skip the async `stream_chat`
> rewrite (do connection reuse + timeouts only), and vendor the web JS assets.

## Bugs & correctness

- [x] **`undo()` can eat merged messages** (`chat_session.py`). Consecutive same-role
  messages are concatenated into one, so the "remove the user message on failed send"
  path in `main.py`/`server.py` can delete earlier user text that was merged in. Track
  the pre-send length instead of calling `undo()`, or make `_append` return whether it
  merged.
- [x] **`load_profile_by_name` exits the process from library code** (`config.py:_load_profile`
  calls `sys.exit(1)`), and `_cmd_profile` papers over it by catching `SystemExit`.
  Raise a proper `ProfileNotFoundError` and handle it at the edges.
- [x] **Web server error path can mis-undo** (`src-web/server.py:run_stream`). If an
  exception fires *after* `add_assistant_message`, the `except` block's `session.undo()`
  removes the just-completed exchange.
- [x] **Web welcome message can show `None`** for the model when the profile has no model
  and `/v1/models` is unreachable (`server.py` interpolates `cfg.profile.model` directly;
  the CLI uses `or "(auto)"`).
- [x] **`_serialise_value` falls back to `repr(v)`** (`config.py`), which can write invalid
  TOML (e.g. for `None`). Skip `None` values or raise.
  → `_serialise_value` no longer exists (the tomlkit switch removed it), but the
  underlying risk remained: a `None` sampler (e.g. `/samplers x null`) can't be
  serialised. `save_profile` now drops `None`-valued samplers.
- [x] **Broad silent exception swallowing.** `except (httpx.HTTPError, Exception)` appears in
  `api.py` and `chat_session.py` — the tuple is redundant and `except Exception: pass`
  hides real bugs (typos, JSON shape changes). Catch specific exceptions and log under
  `--debug`.

## Code quality & refactoring

- [x] **~80 lines duplicated twice in `main.py`** — the ephemeral (`/btw`) streaming block is
  a near-copy of the normal streaming block (Thinking spinner, tail preview, interrupt
  handling, markdown finalization). Extract a `stream_and_render(...)` helper. The same
  duplication exists between `run_stream` and `run_ephemeral_stream` in
  `src-web/server.py`.
- [x] **`/copy` clipboard fallback is dead code** (`main.py:184-215`). `pyperclip` is a hard
  dependency in `pyproject.toml`, so the `except ImportError` branch (~30 lines of
  per-platform subprocess code) never runs. Either drop pyperclip and keep the manual
  code, or keep pyperclip and delete the fallback. Also move clipboard logic out of the
  main loop into `images.py` or a new `clipboard.py`.
- [x] **`server.py` imports `_resolve_limits` from `chatty.main`** — a private helper pulled
  across module boundaries. Move it to `config.py` or `api.py` as public API.
- [x] **`main()` is a 290-line function.** The slash-command result handling, image
  attachment, streaming, and token reporting could each be functions; this would also
  let the web server share more code instead of re-implementing the loop.
- [x] **`AppConfig._raw` is mutated from outside** (`commands.py:_cmd_profile` sets
  `cfg._raw`). Add a proper `switch_profile` method on `AppConfig`.

## Testing

- [x] **No coverage of `main.py`, `ui.py`, or `src-web/server.py`** — the largest and most
  bug-prone files. The FastAPI app is easily testable with `fastapi.testclient`
  (WebSocket support included) + `respx` for the upstream API.
- [x] **No test for context-clipping edge cases** that involve the leading-assistant-drop
  loop, merged messages, or image token costs interacting with the budget.
- [x] Consider `pytest-cov` with a modest threshold to keep new code honest.

## Packaging & distribution

- [x] **The web UI is not installable.** `[tool.hatch.build.targets.wheel]` only packages
  `src/chatty`, so `uv tool install .` ships a `chatty` that can't serve the web UI, and
  the README's `uv run python src-web/server.py` only works from a repo checkout. Move
  `src-web` into the package (e.g. `chatty/web/` with static assets as package data) and
  expose it as `chatty --web` or a `chatty-web` entry point with `--host`/`--port` flags
  (both are currently hardcoded to `127.0.0.1:8000`).
- [x] **Heavy deps are unconditional.** `fastapi`, `uvicorn`, `websockets`, and `pyperclip`
  are required even for pure-CLI use. Make them optional extras: `chatty[web]`,
  `chatty[clipboard]`.
  → `fastapi`/`uvicorn`/`websockets` moved to a `chatty[web]` extra. `pyperclip`
  kept in core (by decision) since `/copy` is a core CLI feature, so no
  `chatty[clipboard]` extra was needed.
- [x] **`websockets` appears unused directly** (FastAPI/uvicorn handle WS) — verify and drop.
  → Verified it is *not* safe to drop: uvicorn needs a WS implementation for
  FastAPI WebSocket support. Kept, but relocated into the `chatty[web]` extra.
- [x] **Offline operation breaks silently**: tiktoken downloads its encoding on first use,
  and the web UI loads marked/KaTeX/DOMPurify from jsdelivr CDNs. Vendor the JS assets
  into `public/` and document/cache the tiktoken requirement.

## Web UI

- [x] **No way to stop generation** — the CLI has Ctrl+C; the web UI has no equivalent. Add
  a stop button that sends a cancel message over the WebSocket (requires making the
  streaming loop interruptible, e.g. a threading.Event checked between chunks).
- [x] **Input is blocked sequentially per connection** — `await asyncio.to_thread(run_stream)`
  means commands sent during generation queue up. Related to the stop-button work.
- [x] **O(n²) markdown re-parsing** — every chunk re-parses the entire response
  (`app.js:137`, already flagged in a comment). Throttle re-rendering (e.g. every 100 ms
  via `requestAnimationFrame`) rather than per-chunk.
- [x] **No image support in the browser.** `/image clipboard` only works because the server
  runs on the same machine. Support paste/drag-and-drop upload over the WebSocket.
- [x] **Session/state polish**: theme choice isn't persisted (localStorage), chat history is
  lost on page reload (server keeps per-connection state only), no reconnect logic when
  the WS drops (just an error message), and no input history (up-arrow).
- [x] **Each WebSocket connection re-parses `sys.argv`** — a second tab gets a fresh session
  (probably fine) but also re-runs `load_config`/`_resolve_limits` including network
  calls; consider doing this once at startup and sharing the config.

## CLI / UX feature ideas

- [x] **Tab completion** for slash commands, profile names, model names (from `/v1/models`),
  and `@` file paths via a prompt_toolkit `Completer`.
- [x] **`/retry` / `/regen`** — resend the last user message (undo + resend), very common in
  chat clients.
- [x] **`/edit`** — edit the last user message in `$EDITOR` and resend.
- [x] **Session management** — `/sessions` to list saved files in `~/.config/chatty/`,
  autosave-on-exit option, session titles/timestamps in the save format.
- [x] **Real token usage** — the final SSE chunk usually carries a `usage` object; prefer it
  over the tiktoken estimate for the `[tokens: ...]` display, and show tokens/sec.
- [x] **Windows support** is partial: clipboard *copy* works (`clip`), clipboard image
  *paste* doesn't (`images.py:get_clipboard_image` returns None on win32). Either add
  PowerShell-based image grabbing or document the limitation.
- [x] **`/ctx` doesn't persist to the profile** — `/genmax` writes to `cfg.profile`, `/ctx`
  doesn't, so `/samplers save` semantics differ subtly between them (it currently
  re-syncs both, but only because `_cmd_samplers` copies session state back). Unify.

## Performance & networking

- [x] **New `httpx.Client` per request** (`api.py:stream_chat`) — no connection reuse; with
  TLS endpoints that's a full handshake per message. Create one client per profile and
  reuse it (also enables HTTP/2).
  → Clients are now cached per `(base_url, api_key)` and reused. HTTP/2 is left
  off (`http2=False`) to avoid adding the `h2` dependency; enabling it later is a
  one-line change plus that extra.
- [x] **`timeout=None` is total** — a stalled server hangs until Ctrl+C. Use
  `httpx.Timeout(connect=10, read=None)` so connection failures surface quickly while
  streaming reads stay unbounded.
- [x] **`/tokenize` is called synchronously per unique message on every send** — fine
  locally, slow against remote endpoints. Consider counting only new/changed messages
  (cache per message id) or batching.
  → Satisfied by the existing per-text LRU cache (unchanged messages are never
  recounted) plus the new pooled client reuse for the `/tokenize` request.
- [x] The web server wraps the sync client in threads; a cleaner long-term path is an
  async variant of `stream_chat` using `httpx.AsyncClient`, shared by CLI (via
  `asyncio.run`) and server. **(Deferred this pass — sync + threads retained by decision.)**
