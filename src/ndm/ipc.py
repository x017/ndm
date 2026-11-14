"""Same-machine daemon IPC. Unix sockets / Windows named pipes; no TCP listener."""
from __future__ import annotations

import hashlib
import os
import secrets
from multiprocessing.connection import AuthenticationError, Client, Listener
from pathlib import Path


def endpoint(data_dir: Path) -> tuple[str, str]:
    digest = hashlib.sha256(str(data_dir.resolve()).encode()).hexdigest()[:20]
    if os.name == "nt":
        return rf"\\.\pipe\ndm-{digest}", "AF_PIPE"
    return str(data_dir / "ndm.sock"), "AF_UNIX"


def authentication_key(data_dir: Path) -> bytes:
    """Persist a private challenge key, not a browser token or network credential."""
    path = data_dir / "ipc.key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        key = path.read_bytes()
        if len(key) != 32:
            raise RuntimeError("Invalid daemon IPC key. Stop NDM and remove ipc.key to reset it.")
        return key
    key = secrets.token_bytes(32)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
    if os.name != "nt":
        path.chmod(0o600)
    return key


def connect(data_dir: Path, *, timeout: float = 30.0):
    address, family = endpoint(data_dir)
    try:
        connection = Client(address, family=family, authkey=authentication_key(data_dir))
    except AuthenticationError as exc:
        raise RuntimeError("The daemon IPC key changed. Close NDM and start it again.") from exc
    # Connection.poll supplies a deadline on both POSIX sockets and Windows pipes.
    connection._ndm_timeout = timeout
    return connection


def request(data_dir: Path, operation: str, payload: dict | None = None, *, timeout: float = 30.0):
    with connect(data_dir, timeout=timeout) as connection:
        connection.send({"operation": operation, "payload": payload or {}})
        if not connection.poll(timeout):
            raise TimeoutError("The local daemon did not respond in time.")
        reply = connection.recv()
    if not isinstance(reply, dict):
        raise RuntimeError("Invalid response from the local daemon.")
    if not reply.get("ok"):
        raise ValueError(reply.get("error", "The local daemon rejected the request."))
    return reply["result"]


def listener(data_dir: Path) -> Listener:
    address, family = endpoint(data_dir)
    if family == "AF_UNIX":
        Path(address).unlink(missing_ok=True)
    result = Listener(address, family=family, authkey=authentication_key(data_dir))
    if family == "AF_UNIX":
        Path(address).chmod(0o600)
    return result
