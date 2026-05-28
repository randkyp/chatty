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
uv tool install .
chatty
```

## Web UI

Chatty also includes a minimalistic, endlessly scrollable "typewriter paper" web application that maintains the same features and configurations as the CLI.

To start the web server:
```bash
uv run python src-web/server.py
```
You can pass the exact same command line arguments as the TUI client (e.g. `uv run python src-web/server.py -p myprofile -m gpt-4o`). Once the server starts, navigate to `http://localhost:8000` in your browser.

- Default input mode is `Enter` to send, `Shift+Enter` for a new line.
- Switch themes on the fly by typing `/theme dark` or `/theme light` in the chat input.

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
- **Image Attachments**: Type `@/path/to/image.png` anywhere in your message to attach an image inline.
- **Ctrl+C** during generation stops the stream gracefully.
- **Ctrl+D** exits.

### Slash Commands

| Command | Description |
|---|---|
| `/quit` | Exit |
| `/clear` | Clear history (keeps system prompt) |
| `/undo` | Remove last user+assistant exchange |
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
