"""NDM command line: native desktop, local daemon, queue control, and sign-in service."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

from .ipc import request
from .models import default_data_dir


def print_result(result) -> None:
    from .models import redact_proxy_url
    if isinstance(result, dict) and isinstance(result.get("settings"), dict):
        result = {**result, "settings": {**result["settings"]}}
        settings = result["settings"]
        if settings.get("proxy_password"):
            settings["proxy_password"] = "***"
        if settings.get("proxy_url"):
            settings["proxy_url"] = redact_proxy_url(settings["proxy_url"])
    print(json.dumps(result, indent=2))


def install_service(data_dir: Path, start: bool, uninstall: bool = False) -> None:
    if sys.platform.startswith("linux"):
        unit = Path.home() / ".config" / "systemd" / "user" / "ndm.service"
        if uninstall:
            subprocess.run(["systemctl", "--user", "disable", "--now", "ndm.service"], check=False)
            unit.unlink(missing_ok=True)
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            print("NDM user service removed. Downloads and preferences are kept.")
            return
        if unit.exists():
            raise ValueError(f"{unit} already exists. Review it or run ndm service uninstall first.")
        unit.parent.mkdir(parents=True, exist_ok=True)
        command = " ".join(shlex.quote(x) for x in [sys.executable, "-m", "ndm", "daemon", "--data-dir", str(data_dir)])
        unit.write_text(f"""[Unit]
Description=NDM native background download engine
After=network-online.target

[Service]
Type=simple
Environment={json.dumps(f"NDM_HOME={data_dir}")}
ExecStart={command}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
""")
        print(f"Installed {unit}")
        if start:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "--user", "enable", "--now", "ndm.service"], check=True)
            print("NDM starts at sign-in. For operation without a login: loginctl enable-linger $USER")
    elif sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "io.ndm.daemon.plist"
        label = f"gui/{os.getuid()}"
        if uninstall:
            subprocess.run(["launchctl", "bootout", label, plist.as_posix()], check=False)
            plist.unlink(missing_ok=True)
            print("NDM LaunchAgent removed. Your files are kept.")
            return
        if plist.exists():
            raise ValueError(f"{plist} already exists. Review it or run ndm service uninstall first.")
        plist.parent.mkdir(parents=True, exist_ok=True)
        with plist.open("wb") as handle:
            plistlib.dump({"Label": "io.ndm.daemon", "ProgramArguments": [sys.executable, "-m", "ndm", "daemon", "--data-dir", str(data_dir)],
                           "EnvironmentVariables": {"NDM_HOME": str(data_dir)}, "RunAtLoad": True,
                           "KeepAlive": {"SuccessfulExit": False}, "StandardOutPath": str(data_dir / "daemon.log"),
                           "StandardErrorPath": str(data_dir / "daemon.log")}, handle)
        print(f"Installed {plist}")
        if start:
            subprocess.run(["launchctl", "bootstrap", label, plist.as_posix()], check=True)
    else:
        print("Windows: create a Task Scheduler task at sign-in running your installed ndm.exe.")
        print(f"Arguments: daemon --data-dir \"{data_dir}\"")
        print("Run as your own user, not administrator.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ndm", description="Native IDM-style download manager built with Nexa UI.")
    sub = parser.add_subparsers(dest="command")

    def common(p):
        p.add_argument("--data-dir", type=Path, default=None, help="State directory (default: NDM_HOME or ~/.local/share/ndm)")

    desktop = sub.add_parser("desktop", help="Open the native desktop; start the local daemon if needed")
    common(desktop)
    daemon = sub.add_parser("daemon", help="Run the background engine with local IPC; no web server")
    common(daemon)
    daemon.add_argument("--block-private", action="store_true", help="Reject private/local-network download targets")
    add = sub.add_parser("add", help="Add a direct URL to the running daemon")
    common(add)
    add.add_argument("url")
    add.add_argument("--filename")
    add.add_argument("--category", choices=["video", "audio", "archive", "image", "document", "other"])
    add.add_argument("--connections", type=int, choices=range(1, 9))
    add.add_argument("--paused", action="store_true")
    spotify = sub.add_parser("spotify", help="Queue a public Spotify track/album/playlist through spotDL")
    common(spotify)
    spotify.add_argument("url")
    spotify.add_argument("--paused", action="store_true")
    for name, help_text in [("status", "Print the queue as JSON"), ("stop", "Stop the background daemon")]:
        p = sub.add_parser(name, help=help_text)
        common(p)
    for name in ("pause", "resume", "remove"):
        p = sub.add_parser(name, help=f"{name.title()} a queued download")
        common(p)
        p.add_argument("id")
    service = sub.add_parser("service", help="Install/remove a Linux or macOS sign-in service")
    common(service)
    service.add_argument("action", choices=["install", "uninstall"])
    service.add_argument("--start", action="store_true")
    args = parser.parse_args(argv)
    command = args.command or "desktop"
    data_dir = (getattr(args, "data_dir", None) or default_data_dir()).expanduser().resolve()
    try:
        if command == "daemon":
            from .daemon import run_daemon
            run_daemon(data_dir=data_dir, allow_private=not args.block_private)
        elif command == "desktop":
            from .desktop import Desktop
            Desktop(data_dir=data_dir).run()
        elif command == "service":
            install_service(data_dir, args.start, args.action == "uninstall")
        elif command == "add":
            print_result(request(data_dir, "add", {"url": args.url, "filename": args.filename, "category": args.category,
                                                   "connections": args.connections, "start_paused": args.paused}))
        elif command == "spotify":
            result = request(data_dir, "spotify.search", {"url": args.url}, timeout=300)
            print(result["message"])
            print_result(request(data_dir, "spotify.queue", {"songs": [i["song"] for i in result["items"]],
                                                             "start_paused": args.paused}))
        elif command in {"pause", "resume", "remove"}:
            print_result(request(data_dir, "action", {"id": args.id, "action": "delete" if command == "remove" else command}))
        else:
            print_result(request(data_dir, "state" if command == "status" else "shutdown"))
    except KeyboardInterrupt:
        pass
    except (ValueError, RuntimeError, OSError, EOFError, subprocess.CalledProcessError) as exc:
        print(f"NDM: {exc}", file=sys.stderr)
        if command not in {"daemon", "desktop", "service"}:
            print("Start the desktop with ndm, or the background daemon with ndm daemon.", file=sys.stderr)
        raise SystemExit(1) from exc
