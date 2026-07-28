# Chatty

An interactive OpenAI-compatible `/v1/chat/completions` client for your TTY.

Be forewarned: this project is entirely vibecoded. :)

## Quick Start

No install needed — run from the project directory:

```bash
uv run chatty   # syncs deps automatically on first run
```

On first run, `chatty` generates a default config at `~/.config/chatty/config.toml`. Open it and add your API keys/endpoints. (Or copy `config.example.toml` there yourself.)

```bash
uv run chatty -p myprofile               # use a named profile
uv run chatty -m gpt-4o -s "Be concise"  # override model & system prompt
uv run chatty -c /path/to/config.toml    # use a specific config file
uv run chatty --debug                    # print raw JSON payloads
```

To install globally:

```bash
uv tool install .            # CLI only
uv tool install '.[web]'     # CLI + web UI
chatty
```

> **Offline note:** `tiktoken` downloads its `cl100k_base` encoding on first use, so the very first run needs network access. The web UI's JS/CSS (marked, KaTeX, DOMPurify) is vendored, so the browser side works fully offline.

## Web UI

A minimalistic, endlessly scrollable "typewriter paper" web app with the same features and config as the CLI.

```bash
chatty --web                       # if installed with the [web] extra
uv run chatty --web                # from a repo checkout
```

Takes the same connection arguments as the CLI, plus `--host` and `--port` (default `127.0.0.1:8000`). Then open `http://localhost:8000`.

- `Enter` to send, `Shift+Enter` for a new line.
- `/theme dark` or `/theme light` switches themes (remembered across reloads).
- **Stop** (or `Esc`) cancels an in-progress generation.
- Paste or drag-and-drop an image to attach it.

## Configuration & Sessions

Config and sessions live in `~/.config/chatty/` by default.

**Config** is resolved from `~/.config/chatty/config.toml`, then `./config.toml`; if neither exists, a default is generated. Override with `-c/--config`.

**Sessions**: `/save [file]` and `/load [file]` default to `~/.config/chatty/session.json(l)`. Relative filenames are looked up in `~/.config/chatty/`, `./`, and the active config directory; absolute or `./`-prefixed paths are taken as-is. Pass `--autosave` to save the session automatically on exit.

### Input

- `Enter` sends by default; `Shift+Enter` (Esc → Enter) adds a new line. Pass `-e`/`--multiline` to flip this: `Enter` adds a new line, submit with `Meta+Enter` (Esc → Enter) or `Ctrl+Enter`.
- **Tab completion** for slash commands, profile/model names, and `@` file paths.
- Type `@/path/to/image.png` anywhere in a message to attach an image.
- **Ctrl+C** stops the stream; **Ctrl+D** exits.

### Slash Commands

| Command | Description |
|---|---|
| `/quit` | Exit |
| `/clear` | Clear history (keeps system prompt) |
| `/undo` | Remove last user+assistant exchange |
| `/retry`, `/regen` | Resend the last user message (drops the old reply) |
| `/edit` | Edit the last user message in `$EDITOR` and resend |
| `/system [text]` | Show/set/disable system prompt |
| `/ctx [n]` | Show/set context size |
| `/genmax [n]` | Show/set max generation tokens |
| `/profile [name]` | Show/switch profile |
| `/samplers ...` | Show/set/remove samplers, or `save` back to the config file |
| `/image [path]` | Attach an image from file path or clipboard |
| `/save [file]` | Save the chat session |
| `/load [file]` | Load a chat session |
| `/sessions` | List saved session files |
| `/copy` | Copy the last assistant response to the clipboard |

## Development

```bash
uv sync --all-groups     # install dev dependencies
uv run pytest            # run tests
uv run ruff check .      # lint
uv run ruff format .     # format
```

`pre-commit` hooks lint and format on `git commit`; run manually with `uv run pre-commit run --all-files`.

## License

MIT — see [LICENSE](LICENSE).
