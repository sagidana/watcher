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
│                                                      │
│  ┌─────────────────────────────────────────────────┐ │
│  │  SQLite (state.db)                              │ │
│  │  - last-seen hashes per watcher                │ │
│  │  - run history & error log                     │ │
│  └─────────────────────────────────────────────────┘ │
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
├── watchers/                  # one YAML file per watcher
│   └── <8hex-id>.yaml
├── state.db                   # SQLite: hashes, run history
└── sessions/                  # saved Playwright browser session files

watcher/                       # source tree (installed package)
├── watcher/                   # core Python package
│   ├── __init__.py
│   ├── main.py                # entrypoint: wires engine + bot, starts loop
│   ├── cli.py                 # `watcher` CLI (install, uninstall, run, watch…)
│   ├── engine.py              # asyncio task manager; one task per watcher
│   ├── bot.py                 # Telegram bot: /start /status /help
│   ├── notifier.py            # Telegram send helpers (unified-diff messages)
│   ├── picker.py              # headed-browser element picker (watcher watch)
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
4. Initialises the SQLite database at `~/.config/watcher/state.db`
5. Writes a `systemd --user` unit file and runs `systemctl --user enable --now watcher`

Logs are written to `/tmp/watcher.log`.

### Uninstall

```bash
watcher uninstall
```

Stops and disables the service and removes the systemd unit file. Your data at
`~/.config/watcher/` is left untouched.

---

## Adding Watchers

Use the visual browser picker:

```bash
watcher watch
```

This opens a headed Chromium window with a floating toolbar injected into every
page. Navigate to the site you want to monitor, click **Pick Element**, hover
to highlight elements, then click to select. A Confirm / Re-pick dialog lets
you review the generated CSS selector before saving.

The watcher is written to `~/.config/watcher/watchers/<id>.yaml` and the
running service picks it up within 10 seconds — no restart needed.

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
```

Each watcher lives in its own file. Edit the file and the engine will pick up
changes on its next rescan (within 10 s). Set `enabled: false` to pause a
watcher without deleting it.

### Service settings (`~/.config/watcher/settings.yaml`)

```yaml
telegram:
  poll_timeout: 30   # seconds for each Telegram long-poll request (1-55)
```

---

## How It Works

### Fetcher

All watchers currently use the **browser fetcher** (headless Chromium via
Playwright with `playwright-stealth`). One persistent browser context is kept
alive per watcher to avoid spawn overhead on every poll cycle.

The fetcher:
1. Navigates to the URL (with `networkidle` timeout, falling back to `domcontentloaded`)
2. Queries the CSS selector
3. Returns `inner_text()`, normalised (collapsed whitespace and blank lines)

### Change Detection

1. Fetch and normalise the target element's text
2. SHA-256 hash the result
3. Compare against the hash stored in `state.db`
4. If different: save the new hash + snapshot, send a Telegram notification
   with an added/removed unified-diff summary, and record a `changed` run
5. If unchanged: record an `ok` run

### Captcha Strategy

1. **Playwright stealth** — patches browser fingerprints (canvas, WebGL,
   headless flags, user-agent). Bypasses Cloudflare JS challenges passively.
2. **Residential IP advantage** — running on your home machine means your IP
   is a clean residential address.
3. **Graceful failure** — errors are logged and recorded in the run history.

---

## Telegram Bot

The bot runs inside the same process as the watcher service and only responds
to messages from your configured `TELEGRAM_CHAT_ID`.

Currently implemented commands:

| Command    | Description                          |
|------------|--------------------------------------|
| `/start`   | Greeting and command list            |
| `/status`  | Confirms the service is running      |
| `/help`    | Shows available commands             |

---

## CLI Reference

| Command            | Description                                          |
|--------------------|------------------------------------------------------|
| `watcher install`  | Interactive install: Telegram setup + systemd service |
| `watcher uninstall`| Stop, disable, and remove the service               |
| `watcher status`   | Show `systemctl --user status watcher`              |
| `watcher run`      | Run in the foreground (development mode)            |
| `watcher watch`    | Open headed browser picker to add a new watcher     |
| `watcher reload`   | `systemctl --user reload-or-restart watcher`        |

---

## Roadmap

### Done
- [x] Project structure, pyproject.toml, CLI skeleton
- [x] `watcher install` / `uninstall` with interactive Telegram wizard
- [x] systemd user service setup and management
- [x] SQLite schema (watchers, snapshots, runs)
- [x] Browser fetcher (Playwright + stealth, persistent context per watcher)
- [x] Diff engine (SHA-256 hash + unified-diff notification)
- [x] Telegram notifier (MarkdownV2 added/removed summary)
- [x] Monitoring engine (dynamic asyncio task per watcher, 10 s rescan)
- [x] Interactive element picker (`watcher watch`)
- [x] Basic Telegram bot (`/start`, `/status`, `/help`)

### Planned
- [ ] RSS fetcher (Tier 1 — prefer when available)
- [ ] HTTP fetcher (Tier 2 — static/server-rendered pages)
- [ ] Advanced bot commands: `/list`, `/pause`, `/resume`, `/check`, `/remove`, `/logs`
- [ ] Per-watcher retry/backoff with failure alerting
- [ ] Session-based authentication (save/replay Playwright session)
- [ ] CAPTCHA solver integration (2captcha / CapSolver)
- [ ] `watcher status` with rich terminal dashboard
