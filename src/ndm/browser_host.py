"""Firefox/Chromium native-messaging host.

The browser launches this process and speaks length-prefixed JSON over stdin/stdout.
It forwards each intercepted download to the local daemon over the existing
authenticated IPC, so NDM still opens no network port and the browser never
learns the IPC key.

Nothing here may print to stdout except protocol frames: the browser treats stray
output as a malformed message and drops the connection.
"""
from __future__ import annotations

import json
import logging
import struct
import sys
from pathlib import Path

from .ipc import request
from .models import default_data_dir

log = logging.getLogger("ndm.browser")

MAX_MESSAGE = 1024 * 1024
EXTENSION_ID = "ndm@x017.github.io"
HOST_NAME = "ndm"


def read_message(stream=None):
    """Return one decoded message, or None at end of stream."""
    stream = stream if stream is not None else sys.stdin.buffer
    header = stream.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack("=I", header)
    if length == 0 or length > MAX_MESSAGE:
        raise ValueError("Native message length is out of range.")
    body = stream.read(length)
    if len(body) < length:
        return None
    return json.loads(body.decode("utf-8"))


def write_message(payload: dict, stream=None) -> None:
    stream = stream if stream is not None else sys.stdout.buffer
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    stream.write(struct.pack("=I", len(body)))
    stream.write(body)
    stream.flush()


def handle(message: dict, data_dir: Path) -> dict:
    """Translate one browser request into a daemon call.

    Every failure is reported as ok=False rather than raised, so the extension
    can leave the browser's own download running instead of cancelling it.
    """
    if not isinstance(message, dict):
        return {"ok": False, "error": "Malformed request."}
    kind = message.get("type")
    if kind == "ping":
        try:
            request(data_dir, "health", timeout=5)
            settings = request(data_dir, "state", timeout=5)["settings"]
        except (OSError, ValueError, RuntimeError, TimeoutError, EOFError, KeyError) as exc:
            return {"ok": False, "error": str(exc), "running": False}
        return {"ok": True, "running": True,
                "enabled": bool(settings.get("browser_integration")),
                "ask": bool(settings.get("browser_ask", True))}
    if kind == "grab":
        payload = {key: message[key] for key in ("url", "kind") if key in message}
        try:
            result = request(data_dir, "browser.grab", payload, timeout=10)
        except (OSError, ValueError, RuntimeError, TimeoutError, EOFError) as exc:
            log.info("grab refused: %s", exc)
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **result}
    if kind == "offer":
        payload = {key: message[key] for key in ("url", "filename", "mime", "size", "page_url")
                   if key in message}
        try:
            result = request(data_dir, "browser.offer", payload, timeout=10)
        except (OSError, ValueError, RuntimeError, TimeoutError, EOFError) as exc:
            log.info("offer refused: %s", exc)
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **result}
    return {"ok": False, "error": "Unknown request type."}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="ndm-browser-host", add_help=False)
    parser.add_argument("--data-dir", type=Path, default=None)
    args, _ = parser.parse_known_args(argv)
    # The browser launches the host with its own environment, so NDM_HOME is not
    # inherited. The installed launcher passes the data dir explicitly.
    data_dir = (args.data_dir or default_data_dir()).expanduser().resolve()
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    while True:
        try:
            message = read_message()
        except (ValueError, UnicodeDecodeError, struct.error) as exc:
            write_message({"ok": False, "error": str(exc)})
            return 1
        if message is None:
            return 0
        try:
            reply = handle(message, data_dir)
        except Exception as exc:  # noqa: BLE001 - the host must never die silently
            log.exception("native host failed")
            reply = {"ok": False, "error": str(exc)}
        # Echo the id so the extension can match replies to pending downloads.
        if isinstance(message, dict) and "id" in message:
            reply["id"] = message["id"]
        write_message(reply)


if __name__ == "__main__":
    raise SystemExit(main())
