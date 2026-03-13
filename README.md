# watcher

A personal background service that monitors web pages and pushes notifications
to you via Telegram when something changes.

---

## Table of Contents

- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Installation](#installation)
- [Adding Watchers](#adding-watchers)
- [Configuration](#configuration)
- [How It Works](#how-it-works)
  - [Fetcher](#fetcher)
  - [Change Detection](#change-detection)
  - [AI Filtering](#ai-filtering)
  - [Captcha Strategy](#captcha-strategy)
- [Telegram Bot](#telegram-bot)
- [CLI Reference](#cli-reference)
- [Roadmap](#roadmap)

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                   watcher (process)                  │
│                                                      │
│  ┌─────────────┐    ┌──────────────────────────────┐ │
│  │   Engine    │───▶│  Watcher Tasks (per watcher) │ │
│  │ (asyncio)   │    │  - Browser fetcher           │ │
│  │ rescan/10s  │    │  - SHA-256 diff              │ │
│  └─────────────┘    └──────────┬───────────────────┘ │
│                                │ change detected      │
│  ┌─────────────┐    ┌──────────▼───────────────────┐ │
│  │ Telegram    │    │  Notifier (Telegram send)    │ │
│  │ Bot Handler │    │  unified-diff summary        │ │
│  │ /start      │    └──────────────────────────────┘ │
│  │ /status     │                                     │
│  │ /help       │                                     │
│  └─────────────┘                                     │
└──────────────────────────────────────────────────────┘
         │
         │ runs as
         ▼
  systemd --user service
  (starts on login, restarts on crash)
```

The entire service runs inside a single `asyncio` event loop. The engine scans
the watchers directory every 10 seconds and maintains one asyncio task per
enabled watcher. The Telegram bot runs concurrently in the same loop.

---

## Project Structure

```
~/.config/watcher/             # all runtime config and data
├── .env                       # TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (written by installer)
├── settings.yaml              # service settings (e.g. Telegram poll_timeout)
└── watchers/                  # one YAML file per watcher + one snapshot file per watcher
    ├── <8hex-id>.yaml
    └── <8hex-id>.snapshot     # last-seen hash + content (plain text, no DB)

watcher/                       # source tree (installed package)
├── watcher/                   # core Python package
│   ├── __init__.py
│   ├── main.py                # entrypoint: wires engine + bot, starts loop
│   ├── cli.py                 # `watcher` CLI (install, uninstall, run, reload…)
│   ├── engine.py              # asyncio task manager; one task per watcher
│   ├── bot.py                 # Telegram bot: commands + inline keyboard UI
│   ├── notifier.py            # Telegram send helpers (unified-diff messages)
│   ├── watchers_config.py     # YAML CRUD for per-watcher config files
│   ├── config.py              # loads .env + settings.yaml into Settings dataclass
│   ├── data/
│   │   └── watcher.service.tmpl  # systemd unit file template
│   └── fetchers/
│       ├── __init__.py
│       └── browser.py         # headless Chromium via Playwright (+ stealth)
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
3. Runs `playwright install chromium`
4. Writes a `systemd --user` unit file and runs `systemctl --user enable --now watcher`

Logs are written to `/tmp/watcher.log`.

### Uninstall

```bash
watcher uninstall
```

Stops and disables the service and removes the systemd unit file. Your data at
`~/.config/watcher/` is left untouched.

---

## Adding Watchers

Create a YAML file in `~/.config/watcher/watchers/`:

```bash
cat > ~/.config/watcher/watchers/mysite.yaml <<EOF
id: mysite
name: "My site price"
url: https://example.com/product
selector: "span.price"
interval: 60
enabled: true
created_at: "2026-01-01T00:00:00+00:00"
EOF
```

The running service picks it up within 10 seconds — no restart needed.

You can also manage watchers interactively via the **Telegram bot** (see below).

---

## Configuration

### Per-watcher YAML (`~/.config/watcher/watchers/<id>.yaml`)

```yaml
id: a1b2c3d4
name: "Example page"
url: https://example.com/page
selector: "div.price"
interval: 30          # seconds between checks
enabled: true
created_at: "2026-01-01T00:00:00+00:00"
prompts:              # optional: chain of cai filter prompts
  - "Only pass through price changes. Return empty if nothing relevant."
```

Each watcher lives in its own file. Edit it and the engine picks up changes on
the next rescan (within 10 s). Set `enabled: false` to pause without deleting.

Snapshots are stored alongside the YAML as `<id>.snapshot` — no database needed.

### Service settings (`~/.config/watcher/settings.yaml`)

```yaml
telegram:
  poll_timeout: 30   # seconds for each Telegram long-poll request (1-55)
```

---

## How It Works

### Fetcher

All watchers use the **browser fetcher** (headless Chromium via Playwright with
`playwright-stealth`). One persistent browser context is kept alive per watcher
to avoid spawn overhead on every poll cycle.

The fetcher:
1. Navigates to the URL (with `networkidle` timeout, falling back to `domcontentloaded`)
2. Queries the CSS selector
3. Returns `inner_text()`, normalised (collapsed whitespace and blank lines)

### Change Detection

1. Fetch and normalise the target element's text
2. SHA-256 hash the result
3. Compare against the hash stored in `~/.config/watcher/watchers/<id>.snapshot`
4. If different: save the new snapshot, build a unified-diff summary, and send a Telegram notification
5. If unchanged: sleep until the next interval

### AI Filtering

Each watcher can define a `prompts` list. When a change is detected the diff is
passed through the prompts in order using `cai`. Each prompt can filter,
summarise, or rewrite the diff before it reaches the next step. If any prompt
returns empty the notification is suppressed — useful for ignoring irrelevant
updates (ads, timestamps, etc.).

### Captcha Strategy

1. **Playwright stealth** — patches browser fingerprints (canvas, WebGL,
   headless flags, user-agent). Bypasses Cloudflare JS challenges passively.
2. **Residential IP advantage** — running on your home machine means your IP
   is a clean residential address.
3. **Graceful failure** — errors are logged; the task retries on the next interval.

---

## Telegram Bot

The bot runs inside the same process as the watcher service and only responds
to messages from your configured `TELEGRAM_CHAT_ID`.

### Commands

| Command      | Description                              |
|--------------|------------------------------------------|
| `/start`     | Greeting and command list                |
| `/status`    | Confirms the service is running          |
| `/help`      | Shows available commands                 |
| `/watchers`  | List all watchers with inline actions    |
| `/files`     | Browse and manage session files          |
| `/clipboard` | Copy text to the host clipboard          |

### Inline watcher management

From `/watchers` you can: enable/disable, rename, change interval, edit
prompts, trigger an immediate fetch, or delete any watcher — all without
touching the YAML files directly.

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

---

## Roadmap

### Done
- [x] Project structure, pyproject.toml, CLI skeleton
- [x] `watcher install` / `uninstall` with interactive Telegram wizard
- [x] systemd user service setup and management
- [x] Browser fetcher (Playwright + stealth, persistent context per watcher)
- [x] File-based snapshot storage (`<id>.snapshot`, no database)
- [x] Diff engine (SHA-256 hash + unified-diff notification)
- [x] Telegram notifier (HTML formatted added/removed summary)
- [x] Monitoring engine (dynamic asyncio task per watcher, 10 s rescan)
- [x] Telegram bot with inline keyboard watcher management
- [x] AI filter chain (`cai` prompt pipeline per watcher)

### Planned
- [ ] RSS fetcher (prefer when available)
- [ ] HTTP fetcher (static/server-rendered pages, no browser needed)
- [ ] Per-watcher retry/backoff with failure alerting
- [ ] Session-based authentication (save/replay Playwright session)
