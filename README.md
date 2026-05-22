# Chatty

Interactive CLI chat client for any OpenAI-compatible `/v1/chat/completions` endpoint.

## Quick Start

No install needed — just run from the project directory:

```bash
cp config.example.toml config.toml   # edit with your endpoint/key
uv run chatty                        # syncs deps automatically on first run
```

### Options

```bash
uv run chatty -p myprofile           # use a named profile
uv run chatty -m gpt-4o -s "Be concise"  # override model & system prompt
uv run chatty --debug                # print raw JSON payloads
```

### Global Install (optional)

If you want `chatty` available everywhere:

```bash
uv tool install .
chatty
```

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
| `/samplers save` | Save runtime config back to config.toml |
| `/image [path]` | Attach an image from file path or clipboard |
| `/save [file]` | Save active chat session to session.json or custom file |
| `/load [file]` | Load chat session from session.json or custom file |

