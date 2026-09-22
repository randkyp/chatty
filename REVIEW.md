# Repository review

## Scope and validation

Reviewed the CLI, configuration/profile handling, API client, session/context management, commands and persistence, image/clipboard utilities, browser client, WebSocket server, and tests. The basic module split is appropriate for a single-user local application; no larger framework or production infrastructure is warranted.

The existing suite passes: **130 tests, 70.13% Python coverage** (Python 3.13, dependencies installed in an isolated `uv` environment). Additional small, in-memory reproductions confirmed the cancellation, DOM undo, tokenizer, load, and profile-switch behaviors below. No live provider or full browser was used; reconnect behavior is established by tracing both ends of the connection.

## High

### [x] H1. Stop cannot interrupt a silent provider

**Files:** `src/chatty/web/server.py` (`_run_stream`, `_stream_to_ws`, `websocket_endpoint`); `src/chatty/api.py` (`STREAM_TIMEOUT`, `stream_chat`).

The cancellation event is checked only **after** the synchronous stream iterator yields an event. Its HTTP read timeout is unlimited. If the provider is thinking without emitting content, or stops responding mid-generation, Stop sets a flag but does not interrupt the blocked read. The current task remains active, so subsequent messages and commands are rejected. Disconnecting also merely sets the flag.

A reproduction with a generator blocked before its first yield confirmed that setting cancellation leaves the worker running until the generator is externally released. This matters particularly for slow local models: Stop is needed most when no tokens are arriving.

**Direction:** Give the web request a genuinely cancellable I/O lifetime, e.g. an async HTTP stream owned by the generation task, with guaranteed response cleanup. Cancelling `asyncio.to_thread` alone will not stop its worker. Also move synchronous tokenization out of the WebSocket event loop: normal sends and retries currently call payload construction directly, potentially blocking receipt of Stop during `/tokenize` requests. Test cancellation before the first token and during a silent interval, followed by a successful next request. The existing cancellation test only sends Stop when nothing is running.

### [x] H2. Browser transcript edits do not correspond to session edits

**Files:** `src/chatty/web/public/app.js` (`sendMessage`, `handleServerMessage`); `src/chatty/commands.py` (`CommandResult`, `_cmd_retry`, `/undo`); `src/chatty/web/server.py` (`_handle_message`).

Every submitted command is rendered as `.message.user`, but `remove_last_exchange` blindly removes the last two user/assistant DOM elements. After a question and answer, typing `/undo` produces `[question, answer, /undo]`; the event removes `/undo` and the answer, leaving the question visible even though the server removed both conversation messages. Executing the actual JavaScript handlers with a minimal DOM substitute reproduced this.

Other paths expose the same boundary problem: `/btw` replies look like ordinary assistant messages although they are not in server history, so a later `/retry` can remove the ephemeral reply instead of the reply actually replaced on the server. Failed or busy-rejected submissions also remain visible as ordinary user turns; `sendMessage` clears their text and image attachments regardless of acceptance.

**Why it matters:** The transcript ceases to represent what the model sees, and basic undo/retry operations visibly edit the wrong content.

**Direction:** Distinguish command output, ephemeral exchanges, pending submissions, and committed conversation turns. Prefer stable turn identifiers or a small authoritative history update over positional DOM deletion flags. Preserve drafts/attachments on rejection. Add executable client tests for `/undo`, `/btw` followed by `/retry`, and submission during generation; current asset tests inspect source strings rather than exercising these transitions.

### [x] H3. Ordinary prompt text containing tokenizer markers can crash the CLI

**Files:** `src/chatty/chat_session.py` (`TokenCounter.count`, `stage_user_message`); `src/chatty/main.py` (normal send loop); `src/chatty/web/server.py` (`_runner`).

The fallback calls `tiktoken.encode(text)` with its default special-token rejection. For example, counting `Explain <|endoftext|>` raises `ValueError`. Such text is plausible when discussing model templates or pasting model output, and the fallback is the normal path for providers without `/tokenize`.

The exception occurs after staging has mutated history and cleared pending images. It is uncaught in the CLI send loop, terminating the application. On web, it escapes the independently created task without a user-facing failure response. Counting assistant output can trigger the same problem in CLI token reporting.

**Direction:** Count arbitrary conversation text as ordinary text (`encode_ordinary` or an explicit equivalent policy). Test special-token strings in user, system, and assistant content. Ensure a preparation failure restores staged state and is surfaced to the user rather than leaving an unobserved task exception.

## Medium

### [x] M1. Automatic reconnect silently starts a different conversation behind the old transcript

**Files:** `src/chatty/web/server.py` (`websocket_endpoint`); `src/chatty/web/public/app.js` (`connect`, `welcome` handling).

Each WebSocket connection creates a fresh `ChatSession` and a fresh config copy. The browser automatically reconnects but retains its transcript; the new welcome message is simply appended. Earlier turns, model/profile switches, and session settings are therefore absent on the server even though the screen still suggests a continuing conversation. A subsequent `/save` saves only the new connection's history. There is no web autosave path despite the shared configuration accepting `--autosave`.

A connection interruption or local server restart is enough to cause this; multiple users are not required. This is not an argument for durable session infrastructure, but the reset must not be implicit.

**Direction:** At minimum, explicitly mark the old transcript as disconnected/non-active and announce that context and settings reset. If seamless reconnect is intended, retain and reattach a small in-memory session while the server lives. Separately clarify or implement the web meaning of `--autosave`. Test a disconnect/reconnect followed by a send and verify both the displayed state and actual payload.

**Dependency:** H2's authoritative transcript updates would make reset handling easier and can eliminate the misleading-display portion of this finding. They do not by themselves preserve session state across connections. If explicit reset is chosen, reconnect persistence is unnecessary.

### [x] M2. Session loading is neither a faithful round trip nor an all-or-nothing operation

**Files:** `src/chatty/commands.py` (`save_session`, `_cmd_load_session`); `tests/test_commands.py`.

Two concrete cases:

- Saving a session without a system prompt writes `"system_prompt": null`. Loading it while another prompt is active leaves that old prompt in place because assignment is conditional on the loaded value being non-null. The restored conversation is silently sent with instructions it did not have when saved.
- Valid JSON with an invalid shape is installed before validation. Loading `{"messages": null}` returns “Failed to load session” only after replacing `session.messages` with `None` (the failure occurs at `len(messages)`). The prior conversation has already been lost from active state. Other invalid message shapes can survive loading and fail on the next send/render.

Both cases were reproduced. The existing round-trip test covers only a non-null system prompt and well-formed messages. Hand-edited or mistakenly selected local JSON is a realistic input here; an elaborate schema framework is not needed.

**Direction:** Parse and validate into temporary values, distinguish an absent metadata field from an explicitly cleared value, then commit the whole restored state once. Check the few invariants consumed by rendering and context building, and reset pending/rollback state consistently. Test null-prompt round trips for JSON and JSONL and verify malformed loads leave the prior session untouched.

### [ ] M3. Profile/model initialization has inconsistent ownership

**Files:** `src/chatty/config.py` (`resolve_limits`, `switch_profile`); `src/chatty/commands.py` (`_cmd_profile`, `apply_model_choice`); `src/chatty/api.py` (`fetch_model_metadata`).

Startup resolves missing limits and model metadata, but `/profile` does not. Switching to a profile with only `base_url` leaves its model unset (requests use the literal `"default"`) and keeps the previous session's context/generation limits. A reproduction switching from limits `12345/678` to such a profile retained those numbers with `cfg.profile.model is None`. Starting directly in the same profile would instead resolve metadata or use defaults.

Additionally, metadata always comes from `models[0]`, even when the configured model names another entry. For multi-model endpoints, automatic clipping limits can therefore belong to the wrong model. `/models` changes only the model string. Ephemeral-key startup also installs fallback values before key entry, with no later resolution step.

**Why it matters:** The active profile and session can disagree, causing arbitrary clipping, provider context errors, or an invalid model request depending on how the user reached the configuration. The metadata problem is conditional on the provider exposing model-specific limits.

**Direction:** Use one small profile-activation/resolution path for startup and switching. Resolve metadata for the selected model, distinguish explicit limits from inferred defaults, and deliberately synchronize the resulting session settings. Defer authenticated discovery until a required key is available. Test switching to an underspecified profile and selecting a non-first catalog model with different limits.

## Fix ordering and overlap

- H1 and H3 are independent; fixing cancellable streaming does not fix tokenizer rejection or preparation rollback.
- Address H2 before polishing individual browser undo/retry branches. A proper distinction between transcript entries and conversation turns makes several positional-deletion patches obsolete. Coordinate M1 with that work as noted above.
- M2 and M3 both update session settings but concern different boundaries: loading a saved conversation versus activating a provider profile. Fixing either alone does not resolve the other.
- The test gaps are attached to their concrete failures above, not separate requests for higher coverage or a larger test infrastructure.
