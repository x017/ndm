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
from .engine import DownloadManager
from .ipc import endpoint, listener, request
from .media import inspect_media
from .models import CaptureRequest, NewDownload, SettingsPatch, default_data_dir
from .store import Store

log = logging.getLogger("ndm")


class SpotifySearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=2048)


class SpotifyQueue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    songs: list[dict] = Field(min_length=1, max_length=500)
    authorized: bool = False
    start_paused: bool = False


class MediaDownload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=8192)
    format_id: str = Field(default="best", max_length=40)
    name: str = Field(default="media", max_length=180)
    category: str = "video"
    quality: str = "best"
    ext: str = Field(default="mp4", max_length=8)
    authorized: bool = False


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
        with connection:
            try:
                message = connection.recv()
                if not isinstance(message, dict):
                    raise ValueError("Invalid local request.")
                result = self.dispatch(message.get("operation"), message.get("payload", {}))
                connection.send({"ok": True, "result": result})
            except (ValidationError, ValueError, KeyError, RuntimeError, OSError) as exc:
                try:
                    connection.send({"ok": False, "error": str(exc)})
                except (OSError, EOFError):
                    pass
            except (AuthenticationError, EOFError, OSError):
                pass
            except Exception:
                log.exception("Unhandled native daemon request")
                try:
                    connection.send({"ok": False, "error": "The daemon could not complete this request. See daemon.log."})
                except (OSError, EOFError):
                    pass

    def dispatch(self, operation: str, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("Request payload must be an object.")
        if operation == "health":
            return {"name": "ndm", "transport": "local-ipc", "pid": os.getpid()}
        if operation == "state":
            result = self.manager.snapshot()
            result["daemon"]["spotify"] = spotify.availability()
            result["daemon"]["transport"] = endpoint(self.data_dir)[1]
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
            if not payload.get("authorized"):
                raise ValueError("Confirm that you own this media or have permission to save it.")
            return inspect_media(url, allow_private=self.manager.allow_private)
        if operation == "media.download":
            return self.manager.add_media(**MediaDownload.model_validate(payload).model_dump())
        if operation == "spotify.search":
            command = SpotifySearch.model_validate(payload)
            return spotify.search(command.url, self.data_dir)
        if operation == "spotify.queue":
            command = SpotifyQueue.model_validate(payload)
            return {"tasks": [self.manager.add_spotify(song, authorized=command.authorized,
                                                     start_paused=command.start_paused) for song in command.songs]}
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    daemon = NativeDaemon(data_dir or default_data_dir(), allow_private=allow_private)

    def stop(signum, frame):
        daemon.stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    log.info("NDM native daemon ready (local IPC; no web server).")
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
