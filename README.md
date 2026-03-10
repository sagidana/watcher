# watcher

A personal background service that monitors web pages and pushes notifications
to you via Telegram when something changes.

---

## Table of Contents

- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Installation](#installation)
- [Configuration](#configuration)
- [How It Works](#how-it-works)
  - [Fetcher Tiers](#fetcher-tiers)
  - [Change Detection](#change-detection)
  - [Captcha Strategy](#captcha-strategy)
  - [Authentication](#authentication)
- [Telegram Bot](#telegram-bot)
- [Roadmap](#roadmap)

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                   watcher (process)                  │
│                                                      │
│  ┌─────────────┐    ┌──────────────────────────────┐ │
│  │  Scheduler  │───▶│  Watcher Tasks (per site)    │ │
│  │ (asyncio)   │    │  - RSS fetcher               │ │
│  └─────────────┘    │  - HTTP fetcher              │ │
│                     │  - Browser fetcher           │ │
│  ┌─────────────┐    └──────────┬───────────────────┘ │
│  │ Telegram    │               │                     │
│  │ Bot Handler │    ┌──────────▼───────────────────┐ │
│  │ (commands)  │    │  Diff Engine                 │ │
│  └──────┬──────┘    │  compare new vs stored state │ │
│         │           └──────────┬───────────────────┘ │
│         │                      │ change detected      │
│         │           ┌──────────▼───────────────────┐ │
│         └──────────▶│  Notifier (Telegram send)    │ │
│                     └──────────────────────────────┘ │
│                                                      │
│  ┌─────────────────────────────────────────────────┐ │
│  │  SQLite (state.db)                              │ │
│  │  - watcher definitions & config                │ │
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

The entire service runs inside a single `asyncio` event loop. The scheduler
fires watcher coroutines at their configured intervals. The Telegram bot runs
concurrently in the same loop, allowing you to send commands that affect live
watcher state without restarting the service.

---

## Project Structure

```
watcher/
├── config/
│   └── watchers.yaml          # your watcher definitions (what to watch)
│
├── storage/
│   ├── state.db               # SQLite: watcher state, hashes, history
│   └── sessions/              # saved Playwright browser session files
│                              # (one per authenticated site)
│
├── watcher/                   # core Python package
│   ├── __init__.py
│   ├── scheduler.py           # asyncio task scheduler, per-watcher intervals
│   ├── diff.py                # compare new content vs stored hash/snapshot
│   ├── extractors.py          # CSS selector / XPath / regex extraction
│   ├── notify.py              # Telegram send helpers
│   ├── db.py                  # aiosqlite database access layer
│   └── fetchers/
│       ├── __init__.py
│       ├── rss.py             # Tier 1: RSS/Atom via feedparser
│       ├── http.py            # Tier 2: plain HTTP via httpx
│       └── browser.py         # Tier 3: headless Chromium via Playwright
│
├── bot.py                     # Telegram bot: command handlers (/add, /list…)
├── main.py                    # entrypoint: wires scheduler + bot, starts loop
├── cli.py                     # `watcher` CLI (install, uninstall, run, status…)
│
├── scripts/
│   └── watcher.service.tmpl   # systemd unit file template
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
- A Telegram bot token (get one from [@BotFather](https://t.me/BotFather) in 30 seconds)
- Your Telegram user/chat ID (send a message to [@userinfobot](https://t.me/userinfobot))

### Steps

```bash
# 1. Clone the repo
git clone <repo> && cd watcher

# 2. Install the package (creates the `watcher` CLI command)
pip install -e .

# 3. Copy and fill in environment variables
cp .env.example .env
$EDITOR .env          # set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID

# 4. Install as a background systemd service
watcher install

# 5. Verify it's running
watcher status
```

`watcher install` does the following automatically:
- Validates that `.env` is present and required variables are set
- Runs `playwright install chromium` to download the headless browser
- Initialises the SQLite database schema
- Writes a `systemd --user` unit file pointing at the current virtualenv
- Runs `systemctl --user daemon-reload && systemctl --user enable --now watcher`

### Uninstall

```bash
watcher uninstall
```

This stops the service, disables it, and removes the systemd unit file. Your
`.env`, `config/`, and `storage/` data are left untouched.

---

## Configuration

Watchers are defined in `config/watchers.yaml`:

```yaml
watchers:
  - id: hn-frontpage
    name: "Hacker News front page"
    url: https://news.ycombinator.com/
    fetcher: rss                    # rss | http | browser
    rss_url: https://news.ycombinator.com/rss
    interval_minutes: 15
    notify_on: new_items            # new_items | any_change | keyword_match
    keywords: ["python", "llm"]     # only notify if these appear (optional)

  - id: some-js-site
    name: "Product stock checker"
    url: https://example.com/product/42
    fetcher: browser
    selector: "#stock-status"       # CSS selector to extract & watch
    interval_minutes: 10
    notify_on: any_change
    auth: session                   # none | session | credentials
    session_file: storage/sessions/example.com.json
```

Each watcher runs independently on its own interval. New watchers can be added
by editing the YAML and running `watcher reload`, or interactively via the
Telegram bot.

---

## How It Works

### Fetcher Tiers

Fetchers are chosen per-watcher. Use the lightest tier that works for the site:

| Tier | Fetcher   | When to use                                      | Speed  |
|------|-----------|--------------------------------------------------|--------|
| 1    | `rss`     | Site has an RSS/Atom feed                        | Fast   |
| 2    | `http`    | Static/server-rendered HTML, no JS needed        | Fast   |
| 3    | `browser` | JavaScript SPA, complex interactions, auth flows | Slow   |
| —    | `api`     | Site has a public API (configure manually)       | Fast   |

**Auto-detection (planned):** On first run for a new URL, watcher can probe for
an RSS feed automatically before falling back to HTTP or browser.

### Change Detection

1. Fetch the page / extract the target element
2. Normalise the content (strip whitespace, sort if unordered)
3. SHA-256 hash the result
4. Compare against the hash stored in `state.db`
5. If different: store the new hash, send a Telegram notification with a diff
   summary

For RSS feeds: track item GUIDs. Notify on new GUIDs only.

### Captcha Strategy

Handled in layers, most permissive first:

1. **Playwright stealth** — patches browser fingerprints (canvas, WebGL,
   headless flags, user-agent). Bypasses Cloudflare JS challenges and basic bot
   detection passively.
2. **Residential IP advantage** — running on your home machine means your IP
   is a clean residential address. Avoid routing traffic through a VPS/proxy
   unless necessary.
3. **CAPTCHA solving service** (2captcha / CapSolver) — optional paid fallback
   configured via `CAPTCHA_API_KEY` in `.env`. Cost is ~$1–3 per 1000 solves;
   negligible for a personal tool.
4. **Graceful failure** — if a watcher fails N consecutive times, it backs off
   and sends you a Telegram alert: _"example.com has been failing for 1 hour —
   check manually."_ No silent failures.

### Authentication

| Method        | How it works                                                     |
|---------------|------------------------------------------------------------------|
| `none`        | No auth. Default.                                                |
| `session`     | You log in manually once in a Playwright browser window, the session is saved to a JSON file, reused on every subsequent run. Re-authenticate manually when it expires. |
| `credentials` | Username/password stored in `.env`, login form automated by Playwright. Not suitable for sites with MFA. |

Session files are stored in `storage/sessions/` and ignored by git.

---

## Telegram Bot

The bot runs inside the same process as the watcher service. You can control
the service by messaging your bot directly.

Planned commands:

| Command             | Description                                      |
|---------------------|--------------------------------------------------|
| `/list`             | Show all active watchers and their status        |
| `/add <url>`        | Add a new watcher interactively                  |
| `/pause <id>`       | Pause a watcher temporarily                      |
| `/resume <id>`      | Resume a paused watcher                          |
| `/check <id>`       | Force an immediate check (don't wait for interval)|
| `/remove <id>`      | Remove a watcher                                 |
| `/status`           | Show service health (uptime, last run times)     |
| `/logs <id>`        | Show recent run history for a watcher            |

The bot only responds to your configured `TELEGRAM_CHAT_ID` — all other
messages are silently ignored.

---

## Roadmap

### Phase 1 — Foundation (current)
- [x] Project structure and README
- [ ] `watcher install` / `watcher uninstall` CLI commands
- [ ] systemd user service setup
- [ ] `.env` validation and first-run checks

### Phase 2 — Core Engine
- [ ] SQLite schema (`db.py`)
- [ ] RSS fetcher
- [ ] HTTP fetcher
- [ ] Diff engine and hash storage
- [ ] Telegram notifier

### Phase 3 — Browser Support
- [ ] Playwright integration
- [ ] Stealth plugin setup
- [ ] Session persistence

### Phase 4 — Bot Commands
- [ ] Telegram bot command handlers
- [ ] Interactive watcher management

### Phase 5 — Robustness
- [ ] Per-watcher retry/backoff
- [ ] Failure alerting
- [ ] CAPTCHA solver integration
- [ ] Auto RSS detection

### Phase 6 — Polish
- [ ] `watcher status` with rich terminal output
- [ ] `watcher logs` tail command
- [ ] Web UI (optional, stretch goal)
