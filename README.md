# Chatty

An interactive OpenAI-compatible `/v1/chat/completions` client for your TTY and browser.

Be forewarned: this project is entirely vibecoded. :)

## Quick Start

Run instantly without installing:
```bash
uv run chatty
```
On first run, `chatty` generates a default config at `~/.config/chatty/config.toml`. Add your API keys/endpoints to it.

To install globally:
```bash
uv tool install .            # CLI only
uv tool install '.[web]'     # CLI + web UI
```

> **Offline Note:** `tiktoken` downloads its `cl100k_base` encoding on first use, so the very first run needs network access. The web UI's JS/CSS (marked, KaTeX, DOMPurify) and fonts (TT2020) are vendored, so the browser side works fully offline.

## Usage

```bash
chatty -p myprofile               # use a named profile
chatty -m gpt-4o -s "Be concise"  # override model & system prompt
chatty -c /path/to/config.toml    # use a specific config file
chatty --autosave                 # save the session on exit (single rolling session.json)
chatty --web                      # launch web UI (default: http://127.0.0.1:8000)
```

## Configuration & Sessions

**Config:** Resolved from `~/.config/chatty/config.toml`, then `./config.toml`. Overridable with `-c/--config`.
**Sessions:** `/save [file]` and `/load [file]` default to `~/.config/chatty/session.json(l)`.
**Autosave:** Pass `--autosave` to write the active session to `~/.config/chatty/session.json` on exit. In the web UI it also saves when a browser disconnects and when the server shuts down. Every autosave overwrites the previous one, so it is a single rolling file — and it shares its path with a bare `/save`, so it replaces that save too.

Profiles use stored keys by default. To keep a provider key only in process memory, opt in explicitly:

```toml
[profile.openai]
base_url = "https://api.openai.com"
api_key_mode = "ephemeral"
model = "gpt-4o"
```

Chatty opens the masked CLI or Web key control when an ephemeral profile starts without a key, including after a profile
switch. You can also run `/apikey` manually. The key is never written to config, sessions,
browser storage, or chat history. It lasts until the CLI or web-server process exits; Web connections and reconnects
share the current key for each profile. `/apikey status` reports whether one is set and `/apikey clear` removes it.
Entering a key does not validate it automatically—use `/models` or send a message to test it with the provider.

The Web control sends the key in plaintext inside the WebSocket connection. Use HTTPS/WSS whenever the browser-to-server
connection crosses an untrusted network. Ephemeral mode is explicit profile opt-in; it does not restrict which connected
browser may replace the profile's process-wide key.

### OpenCode Go

Profiles that talk to OpenCode Go (or a proxy in front of it) should set:

```toml
[profile.opencode]
base_url = "https://opencode.ai/inference/go"
api_key_mode = "ephemeral"
opencode_session = true
```

This sends `x-opencode-session` with a stable id per conversation, and identifies chatty with its own `User-Agent`
instead of the generic HTTP-library one. OpenCode uses both for backend routing and prompt caching; requests without
the header can be rejected with `400 MissingSessionID`. The id is regenerated when a conversation actually starts over
(`/clear`, `/load`) or when the system prompt changes, because the cached prefix no longer applies. It is never written
to config or session files.

### Input Features

- **Send:** `Enter` sends, `Shift+Enter` adds a new line (in CLI use `Esc → Enter`). Flip this with `-e/--multiline`.
- **Images:** Type `@/path/to/image.png` (CLI) or drag-and-drop/paste (Web UI) to attach an image.
- **Completion (CLI):** Tab-complete slash commands, profiles, models, and `@` file paths.
- **Stop:** **Ctrl+C** (CLI) or **Stop / Esc** (Web UI) cancels an in-progress generation.

### Slash Commands

| Command | Description |
|---|---|
| `/help` | Show help summary |
| `/quit`, `/exit` | Exit the application |
| `/clear`, `/newchat` | Clear active chat history (keeps system prompt) |
| `/undo` | Remove the last user/assistant exchange |
| `/retry`, `/regen` | Resend the last user message (drops the old reply) |
| `/edit` | Edit the last user message in `$EDITOR` and resend |
| `/list` | Preview active context window messages |
| `/system [text]` | Show/set/clear the system prompt |
| `/ctx [n]` | Show context window details or set size |
| `/genmax [n]` | Show/set max generation tokens |
| `/timing` | Last request's timing breakdown (app, network, prompt, TTFT, thinking, output) plus usage footer |
| `/profile [name]` | Show active profile or switch connection profile |
| `/apikey [status\|clear]` | Enter, inspect, or clear an ephemeral profile key |
| `/samplers ...` | Show/set/remove samplers, or `save` to config |
| `/reasoning`, `/effort` | Show/set `reasoning_effort` (`off`, `minimal`…`xhigh`, `default`) |
| `/image [path]` | Attach an image from file path or clipboard |
| `/save [file]` | Save active chat session |
| `/load [file]` | Load a chat session |
| `/sessions` | List saved session files |
| `/models [name]` | List available models or switch to one |
| `/btw [msg]` | Send an ephemeral message outside the context window |
| `/theme [mode]` | Switch Web UI theme (`dark` / `light`) |
| `/latex [on\|off\|clear]` | Toggle Web UI LaTeX math rendering (default off) |
| `/copy` | Copy the last assistant response to clipboard |

## Development

```bash
uv sync --all-groups     # install dev dependencies
uv run pytest            # run tests
uv run ruff check .      # lint
uv run ruff format .     # format
```

`pre-commit` hooks lint and format on `git commit`.

## License

MIT — see [LICENSE](LICENSE).
