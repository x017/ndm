"""Register per-browser native-messaging hosts and locate the extensions.

The browser launches the host itself, so the manifest needs an absolute executable
path. NDM is normally run from a project checkout with `uv run ndm` and is not
installed system-wide, so a small launcher is written that pins the interpreter
that ran this command instead of assuming `ndm` is on PATH.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .browser_host import EXTENSION_ID, HOST_NAME

MANIFEST_NAME = f"{HOST_NAME}.json"
BROWSERS = ("firefox", "chrome", "chromium", "brave", "edge")
# Fixed by the public key in extension/chrome/manifest.json, so an unpacked
# load keeps the same id and the host's allowed_origins stays valid.
CHROME_EXTENSION_ID = "cmmjmjhilkeomgcclkdknpkiekeefbbj"

def validate_browser(browser: str) -> str:
    if browser not in BROWSERS:
        raise ValueError("Choose one of: " + ", ".join(BROWSERS))
    return browser


_WINDOWS_VENDORS = {
    "firefox": "Mozilla", "chrome": r"Google\Chrome", "chromium": "Chromium",
    "brave": r"BraveSoftware\Brave-Browser", "edge": r"Microsoft\Edge",
}


def windows_key_path(browser: str) -> str:
    return rf"Software\{_WINDOWS_VENDORS[validate_browser(browser)]}\NativeMessagingHosts\{HOST_NAME}"


_CHROME_DIRS = {
    "chrome": ("google-chrome", "Google/Chrome"),
    "chromium": ("chromium", "Chromium"),
    "brave": ("BraveSoftware/Brave-Browser", "BraveSoftware/Brave-Browser"),
    "edge": ("microsoft-edge", "Microsoft Edge"),
}


def host_manifest_dir(browser: str = "firefox") -> Path:
    validate_browser(browser)
    if browser == "firefox":
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "Mozilla" / "NativeMessagingHosts"
        if os.name == "nt":
            return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "NDM" / "NativeMessagingHosts"
        return Path.home() / ".mozilla" / "native-messaging-hosts"
    linux, mac = _CHROME_DIRS[browser]
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / mac / "NativeMessagingHosts"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "NDM" / browser
    return Path.home() / ".config" / linux / "NativeMessagingHosts"


def extension_dir(browser: str = "firefox") -> Path:
    """The unpacked extension shipped in the checkout (src/ndm/ -> repo root)."""
    validate_browser(browser)
    folder = "firefox" if browser == "firefox" else "chrome"
    return Path(__file__).resolve().parent.parent.parent / "extension" / folder


def launcher_path(data_dir: Path) -> Path:
    return data_dir / ("ndm-browser-host.bat" if os.name == "nt" else "ndm-browser-host")


def write_launcher(data_dir: Path) -> Path:
    """Pin the current interpreter so the host works without a system install."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = launcher_path(data_dir)
    # Never resolve() this: a venv's bin/python is a symlink to the base
    # interpreter, and dereferencing it drops the venv's site-packages, so the
    # host dies with ModuleNotFoundError and Firefox reports "not reachable".
    # abspath makes it absolute without following symlinks.
    python = os.path.abspath(sys.executable)
    target = str(data_dir.expanduser().resolve())
    if os.name == "nt":
        path.write_text(
            f'@echo off\r\n"{python}" -m ndm.browser_host --data-dir "{target}" %*\r\n',
            encoding="utf-8")
    else:
        path.write_text(
            f'#!/bin/sh\nexec "{python}" -m ndm.browser_host --data-dir "{target}" "$@"\n',
            encoding="utf-8")
        path.chmod(0o755)
    return path


def install(data_dir: Path, browser: str = "firefox") -> dict:
    manifest_dir = host_manifest_dir(browser)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    launcher = write_launcher(data_dir)
    manifest = {
        "name": HOST_NAME,
        "description": "NDM download handoff",
        "path": str(launcher),
        "type": "stdio",
    }
    if browser == "firefox":
        manifest["allowed_extensions"] = [EXTENSION_ID]
    else:
        # Chromium calls the same list allowed_origins and matches the id.
        manifest["allowed_origins"] = [f"chrome-extension://{CHROME_EXTENSION_ID}/"]
    path = manifest_dir / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if os.name == "nt":
        register_windows(manifest_dir, browser)
    return {"manifest": str(path), "launcher": str(launcher), "extension": str(extension_dir(browser)),
            "browser": browser}


def register_windows(manifest_dir: Path, browser: str = "firefox") -> None:
    """Windows discovers hosts through the registry rather than a directory."""
    import winreg  # noqa: PLC0415 - Windows only

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, windows_key_path(browser)) as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, str(manifest_dir / MANIFEST_NAME))


def uninstall(data_dir: Path, browser: str = "firefox") -> dict:
    path = host_manifest_dir(browser) / MANIFEST_NAME
    existed = path.exists()
    path.unlink(missing_ok=True)
    if os.name == "nt":
        import winreg  # noqa: PLC0415 - Windows only

        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, windows_key_path(browser))
        except FileNotFoundError:
            pass
    # Every browser shares this launcher. Removing Firefox's registration must
    # not break a still-registered Chrome/Chromium host (and vice versa).
    launcher = launcher_path(data_dir)
    still_used = False
    for other in BROWSERS:
        if other == browser:
            continue
        manifest = host_manifest_dir(other) / MANIFEST_NAME
        try:
            recorded = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if recorded.get("path") == str(launcher):
            still_used = True
            break
    if not still_used:
        launcher.unlink(missing_ok=True)
    return {"removed": existed, "manifest": str(path), "browser": browser,
            "launcher_kept": still_used}


def status(data_dir: Path, browser: str = "firefox") -> dict:
    path = host_manifest_dir(browser) / MANIFEST_NAME
    launcher = launcher_path(data_dir)
    result = {
        "manifest": str(path), "installed": path.is_file(),
        "launcher": str(launcher), "launcher_exists": launcher.is_file(),
        "extension": str(extension_dir(browser)),
        "extension_present": (extension_dir(browser) / "manifest.json").is_file(),
        "browser": browser,
    }
    if path.is_file():
        try:
            recorded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            recorded = {}
        result["registered_path"] = recorded.get("path", "")
        result["path_matches"] = recorded.get("path") == str(launcher)
        result["allowed_extensions"] = recorded.get("allowed_extensions") or recorded.get("allowed_origins", [])
    return result


def doctor(data_dir: Path, browser: str = "firefox") -> dict:
    """Run the launcher the way the browser does and report what actually happens.

    The popup can only say "not reachable"; this reproduces the invocation
    locally so an import error, a bad path, or a stopped daemon is visible.
    """
    import subprocess

    from .browser_host import read_message, write_message
    from .ipc import request

    checks: list[dict] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    manifest_path = host_manifest_dir(browser) / MANIFEST_NAME
    record("host manifest", manifest_path.is_file(), str(manifest_path))
    registered = ""
    if manifest_path.is_file():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            data = {}
            record("manifest readable", False, str(exc))
        registered = data.get("path", "")
        allowed = data.get("allowed_extensions") or data.get("allowed_origins") or []
        wanted = EXTENSION_ID if browser == "firefox" else f"chrome-extension://{CHROME_EXTENSION_ID}/"
        record("extension id allowed", wanted in allowed, ", ".join(allowed))
    launcher = Path(registered or launcher_path(data_dir))
    record("launcher exists", launcher.is_file(), str(launcher))
    if launcher.is_file() and os.name != "nt":
        record("launcher executable", os.access(launcher, os.X_OK), oct(launcher.stat().st_mode & 0o777))
    folder = extension_dir(browser)
    record("extension files", (folder / "manifest.json").is_file(), str(folder))
    try:
        request(data_dir, "health", timeout=5)
        record("daemon running", True, str(data_dir))
    except (OSError, ValueError, RuntimeError, TimeoutError, EOFError) as exc:
        record("daemon running", False, str(exc))

    reply: dict = {}
    if launcher.is_file():
        # Firefox passes the manifest path and add-on ID. Chromium passes the
        # extension origin (plus a parent-window handle on Windows).
        argv = ([str(launcher), str(manifest_path), EXTENSION_ID] if browser == "firefox"
                else [str(launcher), f"chrome-extension://{CHROME_EXTENSION_ID}/"])
        if os.name == "nt" and browser != "firefox":
            argv.append("--parent-window=0")
        try:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
        except OSError as exc:
            record("launcher starts", False, str(exc))
        else:
            record("launcher starts", True)
            try:
                write_message({"type": "ping"}, process.stdin)
                reply = read_message(process.stdout) or {}
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                record("ping reply", False, str(exc))
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
            stderr = process.stderr.read().decode("utf-8", "replace").strip()
            record("host stderr clean", not stderr, stderr[-600:])
            record("ping ok", bool(reply.get("ok")), json.dumps(reply) if reply else "no reply")
            if reply.get("ok"):
                record("browser integration enabled", bool(reply.get("enabled")),
                       "Turn it on: uv run ndm config browser_integration true "
                       "(or Options → Capture → Hand browser downloads to NDM)")
    return {"checks": checks, "reply": reply, "ok": all(c["ok"] for c in checks)}
