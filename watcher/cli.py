"""
watcher CLI — install, uninstall, and manage the watcher service.

Usage:
    watcher install
    watcher uninstall
    watcher status
    watcher run          (foreground, for development)
    watcher reload       (reload watchers.yaml without restart)
"""

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

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


def _write_unit_file() -> None:
    """Generate and write the systemd unit file from the template."""
    template_path = _working_dir() / "scripts" / "watcher.service.tmpl"
    template = template_path.read_text()

    unit_content = template.format(
        working_dir=str(_working_dir()),
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

    # 2. Validate environment
    if not _check_env():
        sys.exit(1)

    # 3. Install Playwright browser
    console.print("\n[bold]Installing Playwright Chromium...[/bold]")
    result = subprocess.run(
        [_python_bin(), "-m", "playwright", "install", "chromium"],
        text=True,
    )
    if result.returncode != 0:
        console.print("[red]✗[/red] Playwright install failed. Check output above.")
        sys.exit(1)
    console.print("[green]✓[/green] Playwright Chromium installed")

    # 4. Initialise database
    console.print("\n[bold]Initialising database...[/bold]")
    _init_db()
    console.print("[green]✓[/green] Database initialised")

    # 5. Write systemd unit
    console.print("\n[bold]Registering systemd service...[/bold]")
    _write_unit_file()

    # 6. Enable and start
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

    args = parser.parse_args()

    commands = {
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "status": cmd_status,
        "run": cmd_run,
        "reload": cmd_reload,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
