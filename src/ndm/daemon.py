"""Headless native daemon. Local authenticated IPC only; never starts a web server."""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
from multiprocessing.connection import AuthenticationError
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import spotify
from .diagnostics import (
    check_connectivity,
    environment,
    log_environment,
    log_failure,
    log_path,
    redact,
    setup_logging,
    tail,
)
from .engine import DownloadManager
from .ipc import endpoint, listener, request
from .media import inspect_media
from .models import (
    SETTINGS_SCHEMA_VERSION,
    CaptureRequest,
    NewDownload,
    SettingsPatch,
    default_data_dir,
)
from .store import Store

log = logging.getLogger("ndm")


def validation_message(exc: ValidationError) -> str:
    """Return a concise, input-free validation error for the desktop/CLI."""
    messages = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = str(error.get("msg", "Invalid value."))
        messages.append(f"{location}: {message}" if location else message)
    return "; ".join(dict.fromkeys(messages)) or "Invalid request."


class SpotifySearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=2048)


class SpotifyQueue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    songs: list[dict] = Field(min_length=1, max_length=500)
    start_paused: bool = False


class MediaDownload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=8192)
    format_id: str = Field(default="best", max_length=40)
    name: str = Field(default="media", max_length=180)
    category: str = "video"
    quality: str = Field(default="best", pattern="^(best|1080|720|audio)$")
    ext: str = Field(default="mp4", max_length=8)
    size: int = Field(default=0, ge=0)
    size_estimated: bool = False


class TaskAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64)
    action: str


class NativeDaemon:
    def __init__(self, data_dir: Path, *, allow_private: bool = True):
        self.data_dir = data_dir.expanduser().resolve()
        self.store = Store(self.data_dir)
        try:
            self.manager = DownloadManager(self.store, allow_private=allow_private)
            self.listener = listener(self.data_dir)
        except Exception:
            self.store.close()
            raise
        self.stop_event = threading.Event()
        self.close_lock = threading.Lock()
        self.closed = False
        self.accept_thread = threading.Thread(target=self._accept, name="ndm-ipc", daemon=True)
        self.accept_thread.start()

    def _accept(self) -> None:
        while not self.stop_event.is_set():
            try:
                connection = self.listener.accept()
            except AuthenticationError:
                continue
            except (OSError, EOFError):
                if not self.stop_event.is_set():
                    log.exception("Local IPC accept failed")
                return
            threading.Thread(target=self._handle, args=(connection,), name="ndm-ipc-request", daemon=True).start()

    def _handle(self, connection) -> None:
        operation = "?"
        with connection:
            try:
                message = connection.recv()
                if not isinstance(message, dict):
                    raise ValueError("Invalid local request.")
                operation = str(message.get("operation") or "?")
                if message.get("stream"):
                    for result in self.dispatch_stream(message.get("operation"), message.get("payload", {})):
                        connection.send({"ok": True, "result": result})
                    connection.send({"ok": True, "done": True})
                else:
                    result = self.dispatch(message.get("operation"), message.get("payload", {}))
                    connection.send({"ok": True, "result": result})
            except ValidationError as exc:
                log.warning("IPC %s rejected: %s", operation, validation_message(exc))
                try:
                    connection.send({"ok": False, "error": validation_message(exc)})
                except (OSError, EOFError):
                    pass
            except (ValueError, KeyError, RuntimeError, OSError) as exc:
                # This is the branch that carries Spotify/TLS/proxy failures. It used to
                # reply to the UI without writing anything, so the log looked empty even
                # though the request had failed. Always record it, with the cause chain.
                log_failure(log, f"IPC {operation}", exc,
                            settings=self.manager.settings.model_dump())
                try:
                    connection.send({"ok": False, "error": str(exc)})
                except (OSError, EOFError):
                    pass
            except (AuthenticationError, EOFError, OSError):
                pass
            except Exception as exc:
                log_failure(log, f"IPC {operation} (unexpected)", exc,
                            settings=self.manager.settings.model_dump())
                log.exception("Unhandled native daemon request")
                try:
                    connection.send({"ok": False, "error": "The daemon could not complete this request. See daemon.log."})
                except (OSError, EOFError):
                    pass

    def dispatch_stream(self, operation: str, payload: dict):
        if not isinstance(payload, dict):
            raise ValueError("Request payload must be an object.")
        log.info("IPC stream %s url=%s", operation, redact(payload.get("url", "")))
        if operation == "spotify.search.stream":
            command = SpotifySearch.model_validate(payload)
            return spotify.search_stream(command.url, self.data_dir,
                                         settings=self.manager.settings.model_dump())
        raise ValueError("Unknown local daemon operation.")

    def dispatch(self, operation: str, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Request payload must be an object.")
        # "state" polls several times a second; logging it would bury everything else.
        if operation not in {"state", "health"}:
            keys = sorted(payload)
            count = len(payload.get("songs") or []) if operation == "spotify.queue" else ""
            log.info("IPC %s%s keys=%s", operation, f" songs={count}" if count != "" else "", keys)
            log.debug("IPC %s payload=%s", operation, redact(payload))
        if operation == "health":
            return {"name": "ndm", "transport": "local-ipc", "pid": os.getpid(),
                    "settings_schema": SETTINGS_SCHEMA_VERSION}
        if operation == "state":
            result = self.manager.snapshot()
            result["daemon"]["spotify"] = spotify.availability()
            result["daemon"]["transport"] = endpoint(self.data_dir)[1]
            result["daemon"]["settings_schema"] = SETTINGS_SCHEMA_VERSION
            result["daemon"]["log_path"] = str(log_path(self.data_dir))
            return result
        if operation == "add":
            return self.manager.add(**NewDownload.model_validate(payload).model_dump())
        if operation == "capture":
            return self.manager.capture(**CaptureRequest.model_validate(payload).model_dump())
        if operation == "action":
            command = TaskAction.model_validate(payload)
            return self.manager.action(command.id, command.action)
        if operation == "settings":
            return self.manager.update_settings(SettingsPatch.model_validate(payload)).model_dump()
        if operation == "media.inspect":
            url = str(payload.get("url", ""))
            if spotify.supported_url(url):
                raise ValueError("Use the Spotify toolbar button for Spotify links.")
            return inspect_media(url, allow_private=self.manager.allow_private,
                                 settings=self.manager.settings.model_dump())
        if operation == "media.download":
            return self.manager.add_media(**MediaDownload.model_validate(payload).model_dump())
        if operation == "spotify.search":
            command = SpotifySearch.model_validate(payload)
            return spotify.search(command.url, self.data_dir, settings=self.manager.settings.model_dump())
        if operation == "spotify.queue":
            command = SpotifyQueue.model_validate(payload)
            return {"tasks": [self.manager.add_spotify(song, start_paused=command.start_paused)
                              for song in command.songs]}
        if operation == "diagnostics":
            lines = int(payload.get("lines", 80))
            return {"path": str(log_path(self.data_dir)), "log": tail(self.data_dir, lines),
                    "environment": environment(self.manager.settings.model_dump())}
        if operation == "diagnostics.check":
            return {"path": str(log_path(self.data_dir)),
                    "results": check_connectivity(self.manager.settings.model_dump())}
        if operation == "library":
            return {"tracks": self.manager.library()}
        if operation == "playlist":
            tracks = self.manager.library()
            return {"content": "#EXTM3U\n" + "\n".join(f"#EXTINF:-1,{Path(t['name']).stem}\n{t['path']}" for t in tracks)}
        if operation == "open":
            task = self.manager.tasks.get(str(payload.get("id")))
            if not task or task["status"] != "completed":
                raise KeyError("Completed download not found.")
            path = Path(task["path"])
            if not path.is_file():
                raise ValueError("The completed file is no longer on disk.")
            if payload.get("folder"):
                path = path.parent
            if sys.platform == "win32":
                os.startfile(str(path))  # noqa: S606
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
            return {"ok": True}
        if operation == "shutdown":
            self.stop_event.set()
            return {"ok": True}
        raise ValueError("Unknown local daemon operation.")

    def serve(self) -> None:
        try:
            while not self.stop_event.wait(0.25):
                pass
        finally:
            self.close()

    def close(self) -> None:
        with self.close_lock:
            if self.closed:
                return
            self.closed = True
            self.stop_event.set()
            self.listener.close()
            address, family = endpoint(self.data_dir)
            if family == "AF_UNIX":
                Path(address).unlink(missing_ok=True)
            self.manager.shutdown()


def run_daemon(*, data_dir: Path | None = None, allow_private: bool = True) -> None:
    directory = (data_dir or default_data_dir()).expanduser().resolve()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    path = setup_logging(directory)
    log_environment(log)
    log.info("Diagnostics log: %s (set NDM_DEBUG=1 for verbose upstream output)", path)
    daemon = NativeDaemon(directory, allow_private=allow_private)
    log_environment(log, daemon.manager.settings.model_dump())

    def stop(signum, frame):
        daemon.stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    log.info("NDM native daemon ready (local IPC; no web server). pid=%s data_dir=%s",
             os.getpid(), directory)
    daemon.serve()


def is_running(data_dir: Path) -> bool:
    try:
        return request(data_dir, "health", timeout=1).get("name") == "ndm"
    except (OSError, ValueError, RuntimeError, EOFError):
        return False


def start_detached(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / "daemon.log").open("ab") as log_file:
        kwargs = {"stdout": log_file, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
                  "env": {**os.environ, "NDM_HOME": str(data_dir)}}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, "-m", "ndm", "daemon", "--data-dir", str(data_dir)], **kwargs)
