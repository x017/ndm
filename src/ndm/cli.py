"""NDM command line: native desktop, independent daemon, companion, and user service."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

import httpx

from .models import default_data_dir
from .server import run_server


def client_for(port: int) -> httpx.Client:
    return httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"X-NDM-Client": "1"}, timeout=20)


def print_response(response: httpx.Response) -> None:
    data = response.json()
    if not response.is_success:
        detail = data.get("detail", "Request failed.")
        if isinstance(detail, list):
            detail = " ".join(d.get("msg", "") for d in detail)
        raise ValueError(detail)
    print(json.dumps(data, indent=2))


def install_service(data_dir: Path, port: int, start: bool, uninstall: bool = False) -> None:
    if sys.platform.startswith("linux"):
        unit = Path.home() / ".config" / "systemd" / "user" / "ndm.service"
        if uninstall:
            subprocess.run(["systemctl", "--user", "disable", "--now", "ndm.service"], check=False)
            unit.unlink(missing_ok=True)
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            print("NDM user service removed. Your downloads and preferences are kept.")
            return
        if unit.exists():
            raise ValueError(f"{unit} already exists. Review it or run ndm service uninstall first.")
        unit.parent.mkdir(parents=True, exist_ok=True)
        command = " ".join(shlex.quote(x) for x in [sys.executable, "-m", "ndm", "daemon", "--port", str(port)])
        unit.write_text(f"""[Unit]
Description=NDM background download engine
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
            print("NDM will start at sign-in. For unattended operation without a login, enable lingering with loginctl enable-linger $USER.")
    elif sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "io.ndm.daemon.plist"
        label = f"gui/{os.getuid()}"
        if uninstall:
            subprocess.run(["launchctl", "bootout", label, str(plist)], check=False)
            plist.unlink(missing_ok=True)
            print("NDM LaunchAgent removed. Your files are kept.")
            return
        if plist.exists():
            raise ValueError(f"{plist} already exists. Review it or run ndm service uninstall first.")
        plist.parent.mkdir(parents=True, exist_ok=True)
        with plist.open("wb") as handle:
            plistlib.dump({"Label": "io.ndm.daemon", "ProgramArguments": [sys.executable, "-m", "ndm", "daemon", "--port", str(port)],
                           "EnvironmentVariables": {"NDM_HOME": str(data_dir)}, "RunAtLoad": True,
                           "KeepAlive": {"SuccessfulExit": False}, "StandardOutPath": str(data_dir / "daemon.log"),
                           "StandardErrorPath": str(data_dir / "daemon.log")}, handle)
        print(f"Installed {plist}")
        if start:
            subprocess.run(["launchctl", "bootstrap", label, str(plist)], check=True)
    else:
        print("Windows: create a Task Scheduler task at sign-in running your installed ndm.exe with arguments: daemon")
        print(f"Set NDM_HOME={data_dir}. Do not run the task as administrator; use your own user account.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ndm", description="Your downloads, in flow. Native Python + Nexa UI.")
    sub = parser.add_subparsers(dest="command")
    def common(p):
        p.add_argument("--data-dir", type=Path, default=None, help="Daemon state directory (default: ~/.local/share/ndm, or NDM_HOME)")
        p.add_argument("--port", type=int, default=8765, help="Local daemon port (default: 8765)")
    desktop = sub.add_parser("desktop", help="Open the native Nexa desktop app; start a daemon if needed")
    common(desktop)
    desktop.add_argument("--screenshot", help="Render headlessly to a PNG instead of opening a window")
    daemon = sub.add_parser("daemon", help="Run the background download engine in this process")
    common(daemon)
    daemon.add_argument("--host", default="127.0.0.1")
    daemon.add_argument("--allow-private", action="store_true", help="Allow local-network targets on a trusted, private daemon")
    web = sub.add_parser("web", help="Serve the browser companion and API")
    common(web)
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--demo", action="store_true", help="Seed clearly labeled local sample transfers")
    web.add_argument("--allow-private", action="store_true")
    add = sub.add_parser("add", help="Add a URL to a running daemon")
    common(add)
    add.add_argument("url")
    add.add_argument("--filename")
    add.add_argument("--category", choices=["video", "audio", "archive", "image", "document", "other"])
    add.add_argument("--connections", type=int, choices=range(1, 9))
    add.add_argument("--paused", action="store_true")
    add.add_argument("--authorized", action="store_true", help="Confirm ownership or permission for audio/video")
    status = sub.add_parser("status", help="Show the running daemon's queue as JSON")
    common(status)
    service = sub.add_parser("service", help="Install or remove a sign-in service (Linux/macOS)")
    common(service)
    service.add_argument("action", choices=["install", "uninstall"])
    service.add_argument("--start", action="store_true", help="Enable and start the service immediately")
    args = parser.parse_args(argv)
    command = args.command or "desktop"
    data_dir = (getattr(args, "data_dir", None) or default_data_dir()).expanduser().resolve()
    port = getattr(args, "port", 8765)
    if not 1 <= port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        if command in {"daemon", "web"}:
            run_server(data_dir=data_dir, host=args.host, port=port,
                       demo=getattr(args, "demo", False), allow_private=getattr(args, "allow_private", False))
        elif command == "desktop":
            from .desktop import Desktop
            Desktop(port=port, data_dir=data_dir, screenshot=args.screenshot).run()
        elif command == "add":
            with client_for(port) as client:
                print_response(client.post("/api/downloads", json={"url": args.url, "filename": args.filename,
                    "category": args.category, "connections": args.connections, "start_paused": args.paused,
                    "authorized": args.authorized}))
        elif command == "status":
            with client_for(port) as client:
                print_response(client.get("/api/state"))
        elif command == "service":
            install_service(data_dir, port, args.start, args.action == "uninstall")
    except KeyboardInterrupt:
        pass
    except (ValueError, RuntimeError, httpx.HTTPError, subprocess.CalledProcessError) as exc:
        print(f"NDM: {exc}", file=sys.stderr)
        if isinstance(exc, httpx.HTTPError):
            print(f"Start the daemon first: ndm daemon --port {port}", file=sys.stderr)
        raise SystemExit(1) from exc
