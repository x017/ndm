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
    web = sub.add_parser("web", help="Open the HTMX/Alpine browser frontend to the local daemon")
    common(web)
    web.add_argument("--host", default="127.0.0.1", help="Bind address; use 0.0.0.0 for network access")
    web.add_argument("--port", type=int, default=8765)
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
    logs = sub.add_parser("logs", help="Show the diagnostics log (use after a failed download)")
    common(logs)
    logs.add_argument("--lines", type=int, default=80, help="How many trailing lines to print")
    logs.add_argument("--path", action="store_true", help="Print only the log file path")
    logs.add_argument("--job", default="", help="Show only lines for one download id (see the queue)")
    logs.add_argument("--grep", default="", help="Show only lines containing this text (e.g. FAILED, STREAM)")
    logs.add_argument("--match", action="store_true",
                      help="Show only the Spotify matching trace (queries, candidates, scores, errors)")
    config = sub.add_parser("config", help="Read or change a setting without opening the desktop")
    common(config)
    config.add_argument("key", nargs="?", help="Setting name, e.g. proxy_remote_dns")
    config.add_argument("value", nargs="?", help="New value: true/false, a number, or text")
    config.add_argument("--list", action="store_true", help="List the network/Spotify settings")
    doctor = sub.add_parser("doctor", help="Test Spotify/YouTube connectivity and show tool versions")
    common(doctor)
    compare = sub.add_parser("match-check", help="Compare the NDM and spotDL matchers without downloading")
    common(compare)
    compare.add_argument("url", help="Public Spotify track, album, or playlist URL")
    compare.add_argument("--limit", type=int, default=10, help="How many tracks to compare")
    for name in ("pause", "resume", "remove"):
        p = sub.add_parser(name, help=f"{name.title()} a queued download")
        common(p)
        p.add_argument("id")
    extension = sub.add_parser("extension", help="Register the browser download/media handoff host")
    common(extension)
    extension.add_argument("action", choices=["install", "uninstall", "status", "doctor"])
    extension.add_argument("--browser", default="firefox",
                           choices=["firefox", "chrome", "chromium", "brave", "edge", "all"],
                           help="Which browser to register (default: firefox)")
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
        elif command == "web":
            from .web import run
            run(data_dir, host=args.host, port=args.port)
        elif command == "desktop":
            from .desktop import Desktop
            Desktop(data_dir=data_dir).run()
        elif command == "extension":
            from . import extension as ext
            targets = list(ext.BROWSERS) if args.browser == "all" else [args.browser]
            if args.action == "install":
                infos = [ext.install(data_dir, target) for target in targets]
                for info in infos:
                    print(f"[{info['browser']}] native host registered → {info['manifest']}")
                print(f"\nLauncher: {infos[0]['launcher']}")
                print("\nLoad the extension:")
                seen = set()
                for target in targets:
                    folder = ext.extension_dir(target)
                    if folder in seen:
                        continue
                    seen.add(folder)
                    if target == "firefox":
                        print("  Firefox: about:debugging#/runtime/this-firefox → Load Temporary Add-on…")
                    else:
                        print(f"  {target.capitalize()}: chrome://extensions → Developer mode → Load unpacked")
                    print(f"    {folder}")
                print("\nTurn the handoff on:")
                print("  uv run ndm config browser_integration true")
                print("\nCheck it with: uv run ndm extension doctor --browser <name>")
                return
            elif args.action == "uninstall":
                for target in targets:
                    info = ext.uninstall(data_dir, target)
                    print(f"[{target}] " + ("removed the native host." if info["removed"]
                                            else "no native host was registered."))
            elif args.action == "doctor":
                failed = False
                for target in targets:
                    report = ext.doctor(data_dir, target)
                    print(f"\n[{target}]")
                    for check in report["checks"]:
                        mark = "OK  " if check["ok"] else "FAIL"
                        print(f"  [{mark}] {check['name']}")
                        if check["detail"] and not check["ok"]:
                            print(f"         {check['detail']}")
                    if report["ok"]:
                        print(f"All checks passed. {target.capitalize()} handoff is ready.")
                        continue
                    failed = True
                    names = {c["name"] for c in report["checks"] if not c["ok"]}
                    print()
                    if names == {"browser integration enabled"}:
                        # Everything is wired up; only the switch is off. Saying
                        # "re-run install" here sent the user round in circles.
                        print("Everything is installed correctly. Turn the handoff on:")
                        print("  uv run ndm config browser_integration true")
                    else:
                        print(f"Something failed above. Fix it, then re-run: uv run ndm extension install --browser {target}")
                        if target == "firefox":
                            print("Then reload the add-on in about:debugging (remove, then Load Temporary Add-on).")
                        else:
                            print(f"Then reload NDM on {target.lower()}://extensions.")
                if failed:
                    raise SystemExit(1)
            else:
                for target in targets:
                    info = ext.status(data_dir, target)
                    print(f"Browser:           {info['browser']}")
                    print(f"Registered:        {'yes' if info['installed'] else 'no'}")
                    print(f"Manifest:          {info['manifest']}")
                    print(f"Launcher present:  {'yes' if info['launcher_exists'] else 'no'}")
                    if info.get("registered_path"):
                        print(f"Host path:         {info['registered_path']}")
                        if not info.get("path_matches"):
                            print("  Warning: the registered path differs from this data dir. Re-run install.")
                    print(f"Extension folder:  {info['extension']}")
                    if not info["extension_present"]:
                        print("  Warning: extension files not found next to the package.")
                    print()
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
        elif command == "match-check":
            from . import spotify
            from .diagnostics import setup_logging
            from .models import Settings
            from .store import Store
            setup_logging(data_dir, rotate=False)
            try:
                settings = Settings.model_validate(Store(data_dir).get_meta("settings", {})).model_dump()
            except Exception:
                settings = Settings().model_dump()
            listing = spotify.search(args.url, data_dir, limit=max(1, args.limit), settings=settings)
            items = listing["items"][:max(1, args.limit)]
            print(f"Comparing {len(items)} track(s). Nothing is downloaded.\n")
            same = differ = errors = 0
            for item in items:
                song = item["song"]
                label = f"{song.get('artist')} - {song.get('name')}"
                outcomes = {}
                for engine in ("spotdl", "ndm"):
                    try:
                        found = spotify.match_song(song, data_dir, stop=None,
                                                   settings={**settings, "spotify_matcher": engine})
                        outcomes[engine] = found["url"]
                    except Exception as exc:
                        outcomes[engine] = f"ERROR {type(exc).__name__}: {str(exc)[:110]}"
                a, b = outcomes["spotdl"], outcomes["ndm"]
                if a.startswith("ERROR") or b.startswith("ERROR"):
                    errors += 1
                    mark = "ERR "
                elif a == b:
                    same += 1
                    mark = "SAME"
                else:
                    differ += 1
                    mark = "DIFF"
                print(f"{mark} {label}")
                print(f"     spotdl: {a}")
                print(f"     ndm   : {b}")
            print(f"\n{same} identical, {differ} different, {errors} errored.")
            if differ:
                print("Open the differing links and listen before switching. To try NDM matching:")
                print("  Options -> Source matcher -> NDM, or set spotify_matcher=ndm in settings.")
        elif command == "logs":
            from .diagnostics import log_path, tail
            if args.path:
                print(log_path(data_dir))
            else:
                # Read a wider window before filtering so --job/--grep can reach back
                # past other jobs' chatter, then keep only the requested trailing lines.
                wanted = max(1, args.lines)
                filtering = bool(args.job or args.grep or args.match)
                window = wanted * 40 if filtering else wanted
                try:
                    result = request(data_dir, "diagnostics", {"lines": window}, timeout=15)
                except (OSError, ValueError, RuntimeError, EOFError):
                    # The daemon may be stopped; the log file is still readable.
                    path, text = log_path(data_dir), tail(data_dir, window)
                else:
                    path, text = result["path"], result["log"]
                if filtering:
                    terms = ("matching", "ytm ", "matched", "match]", "spotdl", "search",
                             "stream", "score", "candidate")
                    rows = [r for r in text.splitlines()
                            if (not args.job or args.job in r)
                            and (not args.grep or args.grep.lower() in r.lower())
                            and (not args.match or any(t in r.lower() for t in terms))]
                    text = "\n".join(rows[-wanted:]) or "No log lines matched that filter."
                print(f"Log: {path}")
                print(text)
        elif command == "config":
            from .models import Settings
            fields = Settings.model_fields
            from .models import redact_proxy_url
            if args.list or not args.key:
                state = request(data_dir, "state", timeout=10)
                current = state.get("settings", {})
                keys = [k for k in fields
                        if k.startswith(("proxy_", "spotify_", "firefox_", "cookie_", "browser_"))]
                for key in keys:
                    value = current.get(key)
                    if key == "proxy_password" and value:
                        value = "***"
                    elif key == "proxy_url" and value:
                        # Credentials can be embedded in the URL itself.
                        value = redact_proxy_url(value)
                    print(f"  {key} = {value}")
                print("\nChange one with: ndm config <key> <value>")
            else:
                key = args.key
                if key not in fields:
                    raise ValueError(f"Unknown setting {key!r}. Run 'ndm config --list'.")
                if args.value is None:
                    state = request(data_dir, "state", timeout=10)
                    value = state.get("settings", {}).get(key)
                    if key == "proxy_password" and value:
                        value = "***"
                    elif key == "proxy_url" and value:
                        value = redact_proxy_url(value)
                    print(f"{key} = {value}")
                else:
                    raw = args.value
                    annotation = fields[key].annotation
                    if "bool" in str(annotation):
                        if raw.strip().lower() not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                            raise ValueError(f"{key} expects true or false, got {raw!r}.")
                        value = raw.strip().lower() in {"true", "1", "yes", "on"}
                    elif "int" in str(annotation):
                        value = int(raw)
                    else:
                        value = raw
                    result = request(data_dir, "settings", {key: value}, timeout=15)
                    # The settings operation returns the Settings dump itself.
                    saved = result if isinstance(result, dict) else {}
                    saved = saved.get("settings", saved)
                    shown = saved.get(key, value)
                    if key == "proxy_password" and shown:
                        shown = "***"
                    elif key == "proxy_url" and shown:
                        shown = redact_proxy_url(str(shown))
                    print(f"{key} = {shown}")
                    if key == "proxy_remote_dns":
                        from .models import redact_proxy_url
                        from .network import proxy_url
                        print(f"Effective proxy URL: {redact_proxy_url(proxy_url(saved))}")
                        print("Restart the daemon so in-flight jobs pick this up: ndm stop && ndm")
        elif command == "doctor":
            from .diagnostics import (
                check_connectivity,
                code_fingerprint,
                environment,
                log_path,
                setup_logging,
            )
            state: dict = {}
            try:
                state = request(data_dir, "state", timeout=10)
                settings = state.get("settings", {})
            except (OSError, ValueError, RuntimeError, EOFError):
                from .models import Settings
                from .store import Store
                setup_logging(data_dir, rotate=False)
                try:
                    settings = Settings.model_validate(Store(data_dir).get_meta("settings", {})).model_dump()
                except Exception:
                    settings = Settings().model_dump()
            print("Versions/tools:")
            for key, value in environment(settings).items():
                print(f"  {key}: {value}")
            disk = code_fingerprint()
            running = (state.get("daemon", {}) if isinstance(state, dict) else {}).get("code_fingerprint")
            print(f"\nCode on disk: {disk}")
            if running is None:
                print("Running daemon: not running, or too old to report its code version.")
                print("  Start it with: ndm stop && ndm")
            elif running != disk:
                print(f"Running daemon: {running}  <-- STALE (old code in memory)")
                print("  Restart it to use the code you just pulled: ndm stop && ndm")
            else:
                print(f"Running daemon: {running}  (up to date)")
            print(f"\nLog file: {log_path(data_dir)}")
            via = "your configured proxy" if settings.get("proxy_enabled") else "a direct connection"
            print(f"\nConnectivity (through {via}):")
            failed = False
            for row in check_connectivity(settings):
                mark = "OK  " if row["ok"] else "FAIL"
                route = f" [via {row['via']}]" if row.get("via") and row["via"] != "direct" else ""
                stack = f" ({row['stack']})" if row.get("stack") else ""
                print(f"  {mark} {row['url']}{route}{stack} — {row['detail']}")
                if row.get("hint"):
                    print(f"       {row['hint']}")
                failed = failed or not row["ok"]
            if failed:
                print("\nAt least one host is unreachable. That is a network/proxy/DNS issue, not a tagging bug.")
                if settings.get("proxy_enabled") and not settings.get("proxy_remote_dns", True):
                    print("Remote DNS is OFF. With a SOCKS proxy, requests resolves hostnames on this")
                    print("machine first, so a censored/poisoned local DNS breaks matching even when")
                    print("the httpx probes pass. Enable remote DNS in Options → Network, then retry.")
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
