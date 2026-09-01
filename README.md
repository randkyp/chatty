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
chatty --web                      # launch web UI (default: http://127.0.0.1:8000)
```

## Configuration & Sessions

**Config:** Resolved from `~/.config/chatty/config.toml`, then `./config.toml`. Overridable with `-c/--config`.
**Sessions:** `/save [file]` and `/load [file]` default to `~/.config/chatty/session.json(l)`. Pass `--autosave` to save on exit.

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
| `/profile [name]` | Show active profile or switch connection profile |
| `/samplers ...` | Show/set/remove samplers, or `save` to config |
| `/image [path]` | Attach an image from file path or clipboard |
| `/save [file]` | Save active chat session |
| `/load [file]` | Load a chat session |
| `/sessions` | List saved session files |
| `/models [name]` | List available models or switch to one |
| `/btw [msg]` | Send an ephemeral message outside the context window |
| `/theme [mode]` | Switch Web UI theme (`dark` / `light`) |
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
