"""
watcher CLI — install, uninstall, and manage the watcher service.

Usage:
    watcher install
    watcher uninstall
    watcher status
    watcher run          (foreground, for development)
    watcher reload       (reload watchers without restart)
    watcher watch        (open browser picker to add a new watcher)
"""

import argparse
import asyncio
import importlib.resources
import logging
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.progress import Progress, SpinnerColumn, TextColumn

console = Console()

# All runtime config/data lives here
CONFIG_DIR = Path.home() / ".config" / "watcher"

# The systemd user unit directory
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
UNIT_NAME = "watcher.service"
UNIT_PATH = SYSTEMD_USER_DIR / UNIT_NAME

LOG_FILE = Path("/tmp/watcher.log")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
    )


def _working_dir() -> Path:
    """Absolute path to the project root (where the package is installed from)."""
    return Path(__file__).resolve().parent.parent


def _python_bin() -> str:
    """Path to the Python interpreter running this process."""
    return sys.executable


def _check_env() -> bool:
    """Validate that ~/.config/watcher/.env exists and required variables are set."""
    env_file = CONFIG_DIR / ".env"
    if not env_file.exists():
        console.print(
            f"[red]✗[/red] [bold].env[/bold] file not found at [cyan]{env_file}[/cyan]\n"
            f"  Copy the example and fill it in:\n"
            f"  [bold]cp .env.example {env_file} && $EDITOR {env_file}[/bold]"
        )
        return False

    required = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"]
    missing = []
    content = env_file.read_text()
    for key in required:
        found = False
        for line in content.splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                value = line.split("=", 1)[1].strip()
                if value and "your-" not in value:
                    found = True
                    break
        if not found:
            missing.append(key)

    if missing:
        console.print(
            f"[red]✗[/red] Missing or unset variables in [bold]{env_file}[/bold]: "
            + ", ".join(f"[yellow]{k}[/yellow]" for k in missing)
        )
        return False

    console.print("[green]✓[/green] .env looks good")
    return True


def _is_env_configured() -> bool:
    """Return True if .env exists with valid TELEGRAM_TOKEN and TELEGRAM_CHAT_ID."""
    env_file = CONFIG_DIR / ".env"
    if not env_file.exists():
        return False
    content = env_file.read_text()
    for key in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
        found = any(
            line.strip().startswith(f"{key}=")
            and (v := line.strip().split("=", 1)[1].strip())
            and "your-" not in v
            for line in content.splitlines()
        )
        if not found:
            return False
    return True


def _setup_telegram() -> None:
    """Interactive wizard to configure Telegram credentials in ~/.config/watcher/.env.

    No-op if .env already has valid values.
    """
    if _is_env_configured():
        console.print("[green]✓[/green] Telegram already configured, skipping setup")
        return

    console.print(
        Panel(
            "[bold]Telegram setup[/bold]\n\n"
            "Open Telegram → message [cyan]@BotFather[/cyan] → [bold]/newbot[/bold] "
            "→ follow prompts → paste the token below.",
            style="blue",
        )
    )

    # --- Stage 1: token entry (up to 3 tries) ---
    token: str | None = None
    bot_username: str | None = None
    for attempt in range(1, 4):
        raw = Prompt.ask("Telegram bot token", password=True)
        raw = raw.strip()
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{raw}/getMe", timeout=10
            )
            data = resp.json()
        except requests.RequestException as exc:
            console.print(f"[red]✗[/red] Network error: {exc}")
            if attempt < 3:
                console.print("  Try again…")
            continue

        if not data.get("ok"):
            desc = data.get("description", "unknown error")
            console.print(f"[red]✗[/red] Telegram rejected the token: {desc}")
            if attempt < 3:
                console.print("  Try again…")
            continue

        token = raw
        bot_username = data["result"]["username"]
        console.print(f"[green]✓[/green] Connected as [bold]@{bot_username}[/bold]")
        break

    if token is None:
        console.print("[red]✗[/red] Could not validate token after 3 attempts. Aborting install.")
        sys.exit(1)

    # --- Stage 2: chat ID discovery (poll getUpdates for up to 60 s) ---
    console.print(
        "\nNow send [bold]any message[/bold] to your bot in Telegram (you have 60 s)…"
    )

    chat_id: str | None = None
    deadline = time.monotonic() + 60
    offset = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
        console=console,
    ) as progress:
        task = progress.add_task("Waiting for message…", total=None)
        while time.monotonic() < deadline:
            remaining = int(deadline - time.monotonic())
            progress.update(task, description=f"Waiting for message… ({remaining}s left)")
            poll_timeout = min(20, max(1, remaining))
            try:
                resp = requests.get(
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    params={"timeout": poll_timeout, "offset": offset},
                    timeout=poll_timeout + 5,
                )
                updates = resp.json().get("result", [])
            except requests.RequestException:
                time.sleep(2)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message") or update.get("channel_post")
                if msg and "chat" in msg:
                    chat_id = str(msg["chat"]["id"])
                    break

            if chat_id is not None:
                break

            if not updates:
                time.sleep(2)

    if chat_id is None:
        console.print(
            "[red]✗[/red] No message received within 60 s.\n"
            "  To find your chat ID manually, send a message to your bot then visit:\n"
            f"  [cyan]https://api.telegram.org/bot{token}/getUpdates[/cyan]\n"
            "  and look for [bold]message.chat.id[/bold] in the JSON."
        )
        sys.exit(1)

    console.print(f"[green]✓[/green] Chat ID found: [bold]{chat_id}[/bold]")

    # --- Stage 3: test message ---
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "✅ Watcher is connected!"},
            timeout=10,
        )
    except requests.RequestException as exc:
        console.print(f"[yellow]~[/yellow] Could not send test message: {exc}")

    confirmed = Confirm.ask("Did you receive the test message?", default=True)
    if not confirmed:
        console.print(
            "[red]✗[/red] Test message not received.\n"
            "  Troubleshooting tips:\n"
            "  • Make sure you sent a message [bold]to[/bold] the bot (not from it)\n"
            "  • Check the bot is not blocked\n"
            "  • Try again with [cyan]watcher install[/cyan] after resolving the issue"
        )
        sys.exit(1)

    # --- Stage 4: write .env ---
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    env_file = CONFIG_DIR / ".env"
    env_file.write_text(f"TELEGRAM_TOKEN={token}\nTELEGRAM_CHAT_ID={chat_id}\n")
    env_file.chmod(0o600)

    console.print(
        Panel(
            f"[green][bold].env written[/bold][/green]\n\n"
            f"  [cyan]{env_file}[/cyan]\n\n"
            f"  TELEGRAM_TOKEN   = (hidden)\n"
            f"  TELEGRAM_CHAT_ID = {chat_id}",
            style="green",
        )
    )


def _ensure_config_dir() -> None:
    """Create ~/.config/watcher and seed watchers.yaml if missing."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / "sessions").mkdir(exist_ok=True)

    yaml_dest = CONFIG_DIR / "watchers.yaml"
    if not yaml_dest.exists():
        yaml_src = _working_dir() / "config" / "watchers.yaml"
        if yaml_src.exists():
            yaml_dest.write_text(yaml_src.read_text())
            console.print(
                f"[green]✓[/green] Created [cyan]{yaml_dest}[/cyan] from example"
            )
        else:
            yaml_dest.write_text("watchers: []\n")
            console.print(f"[green]✓[/green] Created empty [cyan]{yaml_dest}[/cyan]")

    settings_dest = CONFIG_DIR / "settings.yaml"
    if not settings_dest.exists():
        settings_dest.write_text(
            "telegram:\n"
            "  # Seconds Telegram holds each long-poll request open (1-55).\n"
            "  # Higher = fewer API calls; lower = slightly faster cold-start response.\n"
            "  poll_timeout: 30\n"
        )
        console.print(f"[green]✓[/green] Created [cyan]{settings_dest}[/cyan]")


def _write_unit_file() -> None:
    """Generate and write the systemd unit file from the template."""
    ref = importlib.resources.files("watcher.data").joinpath("watcher.service.tmpl")
    template = ref.read_text(encoding="utf-8")

    unit_content = template.format(
        python_bin=_python_bin(),
        config_dir=str(CONFIG_DIR),
        log_file=str(LOG_FILE),
    )

    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    UNIT_PATH.write_text(unit_content)
    console.print(f"[green]✓[/green] systemd unit written to [cyan]{UNIT_PATH}[/cyan]")


def _run_systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
    )


def cmd_install(_args: argparse.Namespace) -> None:
    console.print(Panel("[bold]Installing watcher service[/bold]", style="blue"))

    # 1. Create config dir
    _ensure_config_dir()

    # 2. Telegram setup (interactive wizard if .env missing/incomplete)
    _setup_telegram()

    # 3. Final env guard
    if not _check_env():
        sys.exit(1)

    # 4. Install Playwright browser
    console.print("\n[bold]Installing Playwright Chromium...[/bold]")
    result = subprocess.run(
        [_python_bin(), "-m", "playwright", "install", "chromium"],
        text=True,
    )
    if result.returncode != 0:
        console.print("[red]✗[/red] Playwright install failed. Check output above.")
        sys.exit(1)
    console.print("[green]✓[/green] Playwright Chromium installed")

    # 5. Initialise database
    console.print("\n[bold]Initialising database...[/bold]")
    _init_db()
    console.print("[green]✓[/green] Database initialised")

    # 6. Write systemd unit
    console.print("\n[bold]Registering systemd service...[/bold]")
    _write_unit_file()

    # 7. Enable and start
    _run_systemctl("daemon-reload")
    result = _run_systemctl("enable", "--now", UNIT_NAME)
    if result.returncode != 0:
        console.print(f"[red]✗[/red] systemctl enable failed:\n{result.stderr}")
        sys.exit(1)

    console.print("[green]✓[/green] Service enabled and started\n")
    console.print(
        Panel(
            "[green][bold]watcher is installed and running.[/bold][/green]\n\n"
            f"  Config dir:   [cyan]{CONFIG_DIR}[/cyan]\n"
            f"  Logs:         [cyan]tail -f {LOG_FILE}[/cyan]\n"
            f"  Check status: [cyan]watcher status[/cyan]\n"
            f"  Uninstall:    [cyan]watcher uninstall[/cyan]",
            style="green",
        )
    )


def cmd_uninstall(_args: argparse.Namespace) -> None:
    console.print(Panel("[bold]Uninstalling watcher service[/bold]", style="yellow"))

    result = _run_systemctl("stop", UNIT_NAME)
    if result.returncode == 0:
        console.print("[green]✓[/green] Service stopped")
    else:
        console.print("[yellow]~[/yellow] Service was not running (continuing)")

    result = _run_systemctl("disable", UNIT_NAME)
    if result.returncode == 0:
        console.print("[green]✓[/green] Service disabled")

    if UNIT_PATH.exists():
        UNIT_PATH.unlink()
        console.print(f"[green]✓[/green] Unit file removed: [cyan]{UNIT_PATH}[/cyan]")

    _run_systemctl("daemon-reload")

    console.print(
        f"\n[yellow]Note:[/yellow] Your config and data at "
        f"[cyan]{CONFIG_DIR}[/cyan] have been kept.\n"
        "Run [cyan]watcher install[/cyan] to reinstall."
    )


def cmd_status(_args: argparse.Namespace) -> None:
    result = _run_systemctl("status", UNIT_NAME)
    console.print(result.stdout or result.stderr)


def cmd_run(_args: argparse.Namespace) -> None:
    """Run the watcher in the foreground (development mode)."""
    _setup_logging()
    console.print("[bold]Starting watcher in foreground (Ctrl-C to stop)...[/bold]\n")
    try:
        from dotenv import load_dotenv
        load_dotenv(CONFIG_DIR / ".env")
    except ImportError:
        pass
    from watcher.main import run
    import asyncio
    asyncio.run(run())


def cmd_watch(_args: argparse.Namespace) -> None:
    """Open a headed browser, let the user pick a DOM element, save a watcher."""
    _setup_logging()

    # Load .env so settings are available
    try:
        from dotenv import load_dotenv
        load_dotenv(CONFIG_DIR / ".env")
    except ImportError:
        pass

    # Ensure watchers directory exists
    watchers_dir = CONFIG_DIR / "watchers"
    watchers_dir.mkdir(parents=True, exist_ok=True)

    console.print(
        "[bold]Opening browser…[/bold]\n"
        "Navigate to the page you want to watch, then click [cyan]Confirm[/cyan] "
        "in the toolbar."
    )

    try:
        from watcher.picker import pick_element
        result = asyncio.run(pick_element())
    except RuntimeError as exc:
        console.print(f"[red]✗[/red] {exc}")
        sys.exit(1)

    if result is None:
        console.print("[yellow]~[/yellow] Browser closed without confirming — no watcher added.")
        return

    console.print(f"\n[green]✓[/green] Confirmed page: {result.url}")

    from watcher.watchers_config import WatcherConfig, save

    watcher_id = secrets.token_hex(4)
    name = result.title or result.url
    w = WatcherConfig(
        id=watcher_id,
        name=name,
        url=result.url,
        interval=30,
        enabled=True,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    save(w)

    console.print(
        f"[green]✓[/green] Watcher saved: [bold]{w.name}[/bold]\n"
        f"   File: [dim]{CONFIG_DIR / 'watchers' / (watcher_id + '.yaml')}[/dim]\n"
        "The background service will pick it up within 30 seconds."
    )

    # Signal running service to rescan
    subprocess.run(
        ["systemctl", "--user", "reload-or-restart", UNIT_NAME],
        capture_output=True,
    )


def cmd_reload(_args: argparse.Namespace) -> None:
    result = _run_systemctl("reload-or-restart", UNIT_NAME)
    if result.returncode == 0:
        console.print("[green]✓[/green] Service reloaded")
    else:
        console.print(f"[red]✗[/red] Reload failed:\n{result.stderr}")


def _init_db() -> None:
    """Create config dir and initialise the SQLite schema."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / "sessions").mkdir(exist_ok=True)

    db_path = CONFIG_DIR / "state.db"
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS watchers (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            url         TEXT NOT NULL,
            config_json TEXT NOT NULL,
            enabled     INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            watcher_id  TEXT NOT NULL,
            hash        TEXT NOT NULL,
            snapshot    TEXT,
            checked_at  TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (watcher_id)
        );

        CREATE TABLE IF NOT EXISTS runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            watcher_id  TEXT NOT NULL,
            status      TEXT NOT NULL,   -- ok | changed | error
            detail      TEXT,
            ran_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="watcher",
        description="Personal web monitoring service",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("install", help="Install and start the background service")
    sub.add_parser("uninstall", help="Stop, disable, and remove the service")
    sub.add_parser("status", help="Show service status")
    sub.add_parser("run", help="Run in foreground (development)")
    sub.add_parser("reload", help="Reload watchers config without full restart")
    sub.add_parser("watch", help="Open browser picker to add a new watcher")

    args = parser.parse_args()

    commands = {
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "status": cmd_status,
        "run": cmd_run,
        "reload": cmd_reload,
        "watch": cmd_watch,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
