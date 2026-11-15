"""Register the Firefox native-messaging host and locate the extension.

Firefox launches the host itself, so the manifest needs an absolute executable
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


def host_manifest_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Mozilla" / "NativeMessagingHosts"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "NDM" / "NativeMessagingHosts"
    return Path.home() / ".mozilla" / "native-messaging-hosts"


def extension_dir() -> Path:
    """The unpacked extension shipped in the checkout (src/ndm/ -> repo root)."""
    return Path(__file__).resolve().parent.parent.parent / "extension" / "firefox"


def launcher_path(data_dir: Path) -> Path:
    return data_dir / ("ndm-browser-host.bat" if os.name == "nt" else "ndm-browser-host")


def write_launcher(data_dir: Path) -> Path:
    """Pin the current interpreter so the host works without a system install."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = launcher_path(data_dir)
    python = Path(sys.executable).resolve()
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


def install(data_dir: Path) -> dict:
    manifest_dir = host_manifest_dir()
    manifest_dir.mkdir(parents=True, exist_ok=True)
    launcher = write_launcher(data_dir)
    manifest = {
        "name": HOST_NAME,
        "description": "NDM download handoff",
        "path": str(launcher),
        "type": "stdio",
        "allowed_extensions": [EXTENSION_ID],
    }
    path = manifest_dir / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if os.name == "nt":
        register_windows(manifest_dir)
    return {"manifest": str(path), "launcher": str(launcher), "extension": str(extension_dir())}


def register_windows(manifest_dir: Path) -> None:
    """Windows discovers hosts through the registry rather than a directory."""
    import winreg  # noqa: PLC0415 - Windows only

    key_path = rf"Software\Mozilla\NativeMessagingHosts\{HOST_NAME}"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, str(manifest_dir / MANIFEST_NAME))


def uninstall(data_dir: Path) -> dict:
    path = host_manifest_dir() / MANIFEST_NAME
    existed = path.exists()
    path.unlink(missing_ok=True)
    launcher_path(data_dir).unlink(missing_ok=True)
    if os.name == "nt":
        import winreg  # noqa: PLC0415 - Windows only

        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"Software\Mozilla\NativeMessagingHosts\{HOST_NAME}")
        except FileNotFoundError:
            pass
    return {"removed": existed, "manifest": str(path)}


def status(data_dir: Path) -> dict:
    path = host_manifest_dir() / MANIFEST_NAME
    launcher = launcher_path(data_dir)
    result = {
        "manifest": str(path), "installed": path.is_file(),
        "launcher": str(launcher), "launcher_exists": launcher.is_file(),
        "extension": str(extension_dir()), "extension_present": (extension_dir() / "manifest.json").is_file(),
    }
    if path.is_file():
        try:
            recorded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            recorded = {}
        result["registered_path"] = recorded.get("path", "")
        result["path_matches"] = recorded.get("path") == str(launcher)
        result["allowed_extensions"] = recorded.get("allowed_extensions", [])
    return result
