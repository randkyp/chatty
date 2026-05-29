Act as an expert Python developer. Create a modular, highly maintainable CLI application for interactive chatting with an OpenAI-compatible `/v1/chat/completions` endpoint (e.g., standard OpenAI, vLLM, llama.cpp, or Text Generation WebUI).

Structure the code into logical modules (e.g., `main.py`, `api.py`, `ui.py`, `config.py`, `chat_session.py`). Use modern Python features (type hinting, dataclasses/pydantic).

Here are the detailed requirements:

### 1. User Interface (UI) & Input
- **Plain Text Mode:** Use native terminal scrolling. Do NOT use a full-screen alternate buffer (like `curses`).
- **Input Handling:** Use `prompt_toolkit`. Allow multiline input by default. Send messages via `Ctrl+Enter` (or `Meta+Enter`).
- **Formatting:** Use `rich` for cleanly formatted user/assistant messages (markdown rendering).
- **Graceful Interrupts:** Hitting `Ctrl+C` *during* generation must stop the stream, save the partial response to history, and return to the prompt without crashing. Token counting for the partial response is deferred until the next time a message is sent.
- **Escaping Commands:** If a user message starts with `//`, treat it as a literal `/` and do not parse it as a slash command.

### 2. Configuration & Profiles
- Store configuration in `config.toml`. If the file doesn't exist on launch, generate a default one.
- Profiles contain: `base_url`, `api_key` (optional), `model` (optional), `system_prompt` (optional), `ctx_size` (optional), `genmax` (optional), and `samplers`.
- **Nested Samplers:** Allow *any* arbitrary keys in the TOML samplers section (including nested objects like `chat_template_kwargs` or `extra_body`). Pass these through recursively into the JSON payload.
- Omitted/removed samplers must not be sent in the payload.

### 3. Command-Line Arguments
Use `argparse` (or a modern equivalent) to handle the following startup arguments:
- `--profile`, `-p`: Specify which profile to load from the config file (defaults to `default`).
- `--config`, `-c`: Specify a custom path to the `config.toml` file.
- `--model`, `-m`: Override the model name for the current session.
- `--system`, `-s`: Override the system prompt for the current session.
- `--debug`: Enable debug mode. When active, print the exact JSON payload being sent to the API before streaming the response.
- `--enter-sends`, `-e`: Enable immediate message submission on pressing Enter, using Shift+Enter (Esc then Enter) to insert newlines.
- `--raw`, `-r`: Output raw assistant streaming responses directly to stdout instead of rendering them via Markdown formatting.

### 4. API & Context Management
- Maintain an internal list of message dictionaries (`[{"role": "user", "content": "..."}, ...]`).
- **Dynamic Limits:** If `ctx_size` or `genmax` are not provided in the profile, attempt to fetch them from the server on initialization (e.g., via `/v1/models` metadata). If unavailable, use sensible fallbacks (e.g., 8192 for context, 0 for genmax).
- **Explicit Context Clipping:** Before sending, clip history to fit within `ctx_size - genmax` (reserving room for the response).
  - *Token Counting:* To count tokens, the client must first attempt to call the server's `/tokenize` endpoint (common in local backends like llama.cpp). If the server does not support it (e.g., returns 404 or a connection error), fallback to using `tiktoken` (e.g., `cl100k_base`). Each attached image contributes a 1000-token safe buffer cost to the token estimate.
  - *Message Concatenation:* Consecutive `user` or `assistant` messages are disallowed and must be concatenated into a single message. If messages contain attached images, they are combined structurally as a list of text and image_url blocks.
  - *Priority 1:* The system message is always sent at the top of the context. There should only be zero or one system message in the entire context.
  - *Priority 2:* Keep the newest messages. Always drop oldest `user`/`assistant` pairs until the token count fits. Ensure the resulting history always starts with a `system` then `user`, or simply `user` (never an `assistant` as the first message).
  - If the system prompt + latest user message exceeds the limit, show a `rich` warning and do not send the request.
- **Networking:** Use `httpx` with `timeout=None`. Handle API connection errors gracefully (print the error, return to input prompt, do not crash).
- Stream the response to the terminal.

### 5. Slash Commands
Parse everything after the command as the argument (e.g., `/system You are a bot`).
- `/quit`: Exits safely.
- `/clear`: Empties history (retains system prompt).
- `/undo`: Safely removes the last user+assistant message pair (handle empty history gracefully).
- `/system [text]`: Shows the system prompt or overwrites the system prompt when called with `text`. `disable`/`none` removes it entirely.
- `/ctx [int]` and `/genmax [int]`: Updates context/generation token limits.
- `/profile [name]`: Switches the active profile at runtime.
- `/samplers show`: Prints active samplers (including nested JSON).
- `/samplers disable`: Clears all active samplers for the session.
- `/samplers [key] [value]`: Updates a sampler. Support dot notation (e.g., `/samplers chat_template_kwargs.add_generation_prompt true`) and parse booleans/numbers natively.
- `/samplers rm [key]`: Removes a sampler (supports dot notation).
- `/image [path|clipboard]`: Attach an image from a local path or system clipboard (defaults to clipboard if empty).
- `/save`: Saves the current session's runtime configuration (system prompt, active samplers, limits) back to the active profile in `config.toml`.

### 6. Multimodal Image Attachments
- **Image Scanning:** Scan user input text for `@path` patterns (supporting unquoted/quoted paths and escaped spaces) pointing to existing local image files. If a valid image file is resolved, automatically base64-encode it and strip the `@path` text pattern from the final prompt message.
- **Clipboard Integration:**
  - On macOS, retrieve images using AppleScript (`osascript`) and convert to PNG if needed (using standard macOS tool `sips`).
  - On Linux, retrieve images using system clipboard utilities (`wl-paste` or `xclip`).
- **Payload Format:** When one or more images are attached, structuralise the message `content` as a list of parts, with `{"type": "text", "text": "..."}` and `{"type": "image_url", "image_url": {"url": "data:<mime>;base64,..."}}`.

### 7. Packaging
Provide a `pyproject.toml` configured for `uv`. Include dependencies (`prompt_toolkit`, `rich`, `httpx`, `tiktoken`, `tomli`/`tomllib`) and define a CLI entry point (e.g., `chatty`) so it can be installed via `uv tool install`. Do not provide a `requirements.txt`.

Generate the code, split by file. Add brief comments explaining module interactions.