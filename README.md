# Chatty

An interactive OpenAI-compatible `/v1/chat/completions` client for your TTY.

Be forewarned: this project is entirely vibecoded. :)

## Quick Start

No install needed — just run from the project directory:

```bash
# syncs deps automatically on first run
uv run chatty
```

On your first run, `chatty` will automatically generate a default configuration file at `~/.config/chatty/config.toml`. Simply open it and add your API keys/endpoints!

Alternatively, you can manually copy the provided template to get started:
```bash
mkdir -p ~/.config/chatty
cp config.example.toml ~/.config/chatty/config.toml
```

### Options

```bash
uv run chatty -p myprofile               # use a named profile
uv run chatty -m gpt-4o -s "Be concise"  # override model & system prompt
uv run chatty -c /path/to/config.toml    # explicitly specify a config file
uv run chatty --debug                    # print raw JSON payloads
```

### Global Install (optional)

If you want `chatty` available everywhere:

```bash
uv tool install .            # CLI only
uv tool install '.[web]'     # CLI + web UI
chatty
```

> **Offline note:** `tiktoken` downloads its `cl100k_base` encoding on first use,
> so the very first run (used for token counting fallback) needs network access.
> The web UI's JavaScript/CSS (marked, KaTeX, DOMPurify) is **vendored** under
> `chatty/web/public/vendor/`, so the browser side works fully offline.

## Web UI

Chatty also includes a minimalistic, endlessly scrollable "typewriter paper" web application that maintains the same features and configurations as the CLI. It is part of the package (install the `web` extra) rather than a loose script.

To start the web server:
```bash
chatty --web                       # if installed with the [web] extra
uv run chatty --web                # from a repo checkout
python -m chatty.web.server        # equivalent module entry point
```
You can pass the same connection arguments as the CLI (e.g. `chatty --web -p myprofile -m gpt-4o`) plus `--host` and `--port` (default `127.0.0.1:8000`). Config is loaded once at startup and shared across browser tabs. Once the server starts, navigate to `http://localhost:8000`.

- Default input mode is `Enter` to send, `Shift+Enter` for a new line.
- Switch themes on the fly by typing `/theme dark` or `/theme light`; your choice is remembered across reloads.
- Press **Stop** (or `Esc`) to cancel an in-progress generation.
- Paste or drag-and-drop an image into the input to attach it.
- Chat history is restored on reload and the connection auto-reconnects if dropped.

## Configuration & Sessions

`chatty` stores its config and chat sessions in the standard `~/.config/chatty/` directory by default.

### Config File
On startup, `chatty` resolves the configuration file (default: `config.toml`) by checking:
1. `~/.config/chatty/config.toml`
2. `./config.toml` (local current directory)

If neither exists, a default config is automatically generated at `~/.config/chatty/config.toml`. You can then edit it with your provider endpoints, model profiles, and API keys.

You can override the config path using the `-c` or `--config` flag.

### Chat Sessions
* **Saving**: `/save [file]` saves to `~/.config/chatty/` (defaulting to `session.json` or `session.jsonl` if no file is provided). Absolute paths or paths starting with `./` or `../` are resolved relative to the current directory.
* **Loading**: `/load [file]` loads a saved session. If you specify a relative filename (e.g. `mychat.json`), it checks:
  1. `~/.config/chatty/mychat.json`
  2. `./mychat.json`
  3. `<active_config_dir>/mychat.json`
  
  If no file is specified, `/load` automatically scans these locations in order for a default `session.json` or `session.jsonl`.

### Input

- **Multiline** by default — press `Enter` for a new line.
- **Submit** with `Meta+Enter` (Esc → Enter) or `Ctrl+Enter`.
- **Tab completion** for slash commands, `/profile` names, `/models` names, and `@` file paths.
- **Image Attachments**: Type `@/path/to/image.png` anywhere in your message to attach an image inline.
- **Ctrl+C** during generation stops the stream gracefully.
- **Ctrl+D** exits.

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
| `/samplers show` | Show active samplers |
| `/samplers disable` | Clear all samplers |
| `/samplers key value` | Set a sampler (dot notation supported) |
| `/samplers rm key` | Remove a sampler |
| `/samplers save` | Save runtime config back to active config file |
| `/image [path]` | Attach an image from file path or clipboard |
| `/save [file]` | Save active chat session (defaults to `~/.config/chatty/session.json`) |
| `/load [file]` | Load chat session (looks in `~/.config/chatty/`, `./`, and config directory) |
| `/sessions` | List saved session files in `~/.config/chatty/` |
| `/copy` | Copy the last assistant response to the clipboard |

Pass `--autosave` to save the session to `~/.config/chatty/session.json` automatically on exit.

## Development

`chatty` uses `uv` for Python package and dependency management.

### Setup and Testing

To install development dependencies:
```bash
uv sync --all-groups
```

To run the test suite:
```bash
uv run pytest
```

### Code Quality (Linting & Formatting)

We use [Ruff](https://github.com/astral-sh/ruff) for extremely fast linting and code formatting.

To check for lint errors:
```bash
uv run ruff check .
```

To automatically format the code:
```bash
uv run ruff format .
```

### Pre-commit Hooks

We use `pre-commit` to ensure all changes are automatically linted and formatted before being committed. Git hooks are installed and will run on `git commit`.

If you ever need to manually run pre-commit on all files:
```bash
uv run pre-commit run --all-files
```

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
