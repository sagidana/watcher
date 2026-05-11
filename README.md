# watcher

A personal background service that runs `cai` on a schedule and pushes the
result to you via Telegram.

Each watcher invokes `cai` at a configured interval with your model, tools,
system prompt, and a chain of prompts — and sends the final response straight
to your chat.

---

## Table of Contents

- [Project Structure](#project-structure)
- [Installation](#installation)
- [Configuration](#configuration)
- [How It Works](#how-it-works)
- [Telegram Bot](#telegram-bot)
- [CLI Reference](#cli-reference)
- [Roadmap](#roadmap)

---

## Project Structure

```
~/.config/watcher/             # all runtime config and data
├── .env                       # TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (written by installer)
├── settings.yaml              # service settings (e.g. Telegram poll_timeout)
└── watchers/                  # one YAML file per watcher
    └── <8hex-id>.yaml

watcher/                       # source tree (installed package)
├── watcher/                   # core Python package
│   ├── __init__.py
│   ├── main.py                # entrypoint: wires engine + bot, starts loop
│   ├── cli.py                 # `watcher` CLI (install, uninstall, run, reload…)
│   ├── engine.py              # asyncio task manager; one task per watcher
│   ├── bot.py                 # Telegram bot: commands + inline keyboard UI
│   ├── notifier.py            # Telegram send helper
│   ├── watchers_config.py     # YAML CRUD for per-watcher config files
│   ├── config.py              # loads .env + settings.yaml into Settings dataclass
│   └── data/
│       └── watcher.service.tmpl  # systemd unit file template
│
├── pyproject.toml             # package definition, dependencies, cli entrypoint
├── .env.example               # environment variable template
└── README.md
```

---

## Installation

### Prerequisites

- Python 3.11+
- `pip` or `pipx`
- `cai` available on the same Python interpreter (the engine resolves it from
  `<sys.executable>/../cai`)
- A Telegram account (the installer guides you through bot creation)

### Steps

```bash
# 1. Clone the repo
git clone <repo> && cd watcher

# 2. Install the package (creates the `watcher` CLI command)
pip install -e .

# 3. Install as a background systemd service
watcher install
```

`watcher install` does the following automatically:

1. Creates `~/.config/watcher/` and seeds `watchers/` and `settings.yaml`
2. Runs an **interactive Telegram wizard**:
   - Prompts for your bot token (validated against the Telegram API)
   - Polls for an incoming message to discover your chat ID automatically
   - Sends a test message to confirm everything works
   - Writes `~/.config/watcher/.env` (mode `0600`)
3. Runs `playwright install chromium` (still required by the `pdf2docx` command)
4. Writes a `systemd --user` unit file and runs `systemctl --user enable --now watcher`

Logs are written to `/tmp/watcher.log`.

### Uninstall

```bash
watcher uninstall
```

Stops and disables the service and removes the systemd unit file. Your data at
`~/.config/watcher/` is left untouched.

---

## Configuration

### Per-watcher YAML (`~/.config/watcher/watchers/<id>.yaml`)

```yaml
id: a1b2c3d4
name: "Cyber security daily"
interval: 86400        # seconds between runs
enabled: true
created_at: "2026-01-01T00:00:00+00:00"
model: google/gemini-3-flash-preview
system_prompt: ""      # empty -> default ("time is <now>, you are a professions web searcher and investigator")
tools:
  - fetch_url
prompts:
  - "make a report for me with the latest cyber security events, highlights of the last day, and if extremely important add the last week highlights as well."
```

Each watcher lives in its own file. Edit it and the engine picks up changes on
the next rescan (within 10 s). Set `enabled: false` to pause without deleting.

#### Available models

The bot's model picker exposes:

- `google/gemini-3-flash-preview` (default)
- `anthropic/claude-opus-4.7`
- `anthropic/claude-opus-4.6`
- `google/gemini-pro-latest`
- `openai/gpt-5.5`
- `google/gemma-4-31b-it`

Edit `AVAILABLE_MODELS` in `watcher/watchers_config.py` to extend the list.

#### Available tools

Currently:

- `fetch_url`

Edit `AVAILABLE_TOOLS` in `watcher/watchers_config.py` to extend the list.

### Service settings (`~/.config/watcher/settings.yaml`)

```yaml
log_level: DEBUG

telegram:
  # Seconds Telegram holds each long-poll request open (1-55).
  poll_timeout: 30
```

---

## How It Works

For each enabled watcher, the engine runs an asyncio task that loops:

1. Build the system prompt — the watcher's `system_prompt` if set, otherwise
   the default `"time is <current time>, you are a professions web searcher and investigator"`.
2. Run the **prompt chain** through `cai`:
   - First prompt: `cai --model <m> --tools <t…> --system-prompt <sp> -- <prompt1>`
   - Subsequent prompts: same flags plus `--file <prev_output>` then the next prompt
   - If any step returns empty, the chain stops and no notification is sent
3. Send the final non-empty output to Telegram.
4. Sleep `interval` seconds and repeat.

The watchers directory is rescanned every 10 s — adds, removals, disables, and
config changes are picked up without restarting the service.

---

## Telegram Bot

The bot runs inside the same process as the watcher service and only responds
to messages from your configured `TELEGRAM_CHAT_ID`.

### Commands

| Command      | Description                              |
|--------------|------------------------------------------|
| `/watchers`  | List all watchers with inline actions    |
| `/files`     | Browse and manage session files          |
| `/clipboard` | Copy text to the host clipboard          |
| `/pdf2docx`  | Convert a PDF to DOCX                    |

### Inline watcher management

From `/watchers` you can: create a new watcher, enable/disable, rename, change
interval, edit the prompt chain, change the model, edit the system prompt,
toggle tools, run on-demand, or delete — all without touching the YAML files
directly.

---

## CLI Reference

| Command              | Description                                           |
|----------------------|-------------------------------------------------------|
| `watcher install`    | Interactive install: Telegram setup + systemd service |
| `watcher uninstall`  | Stop, disable, and remove the service                 |
| `watcher status`     | Show `systemctl --user status watcher`                |
| `watcher run`        | Run in the foreground (development mode)              |
| `watcher reload`     | `systemctl --user reload-or-restart watcher`          |
| `watcher message`    | Send a Telegram message or file to your chat          |
| `watcher pdf2docx`   | Convert a PDF file to DOCX                            |

---

## Roadmap

### Done
- [x] Project structure, pyproject.toml, CLI skeleton
- [x] `watcher install` / `uninstall` with interactive Telegram wizard
- [x] systemd user service setup and management
- [x] Telegram notifier
- [x] Monitoring engine (dynamic asyncio task per watcher, 10 s rescan)
- [x] Telegram bot with inline keyboard watcher management
- [x] cai-driven watchers (model / tools / system prompt / prompt chain)
