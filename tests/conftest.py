from __future__ import annotations

import http.server
import threading
import time

import pytest

from ndm.engine import DownloadManager
from ndm.models import SettingsPatch
from ndm.store import Store


class FileServer:
    def __init__(self, payload: bytes, *, ranges: bool = True, delay: float = 0):
        self.payload = payload
        self.ranges = ranges
        self.delay = delay
        self.etag = '"version-1"'
        self.requests = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.requests.append(self.headers.get("Range"))
                data = owner.payload
                start, end = 0, len(data) - 1
                partial = False
                range_header = self.headers.get("Range")
                if owner.ranges and range_header:
                    value = range_header.removeprefix("bytes=")
                    first, last = value.split("-")
                    start = int(first)
                    end = min(int(last), len(data) - 1) if last else len(data) - 1
                    if start >= len(data):
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{len(data)}")
                        self.end_headers()
                        return
                    partial = True
                self.send_response(206 if partial else 200)
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Accept-Ranges", "bytes" if owner.ranges else "none")
                self.send_header("ETag", owner.etag)
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                self.end_headers()
                try:
                    for offset in range(start, end + 1, 65536):
                        self.wfile.write(data[offset:min(offset + 65536, end + 1)])
                        self.wfile.flush()
                        if owner.delay:
                            time.sleep(owner.delay)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/file.bin"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


@pytest.fixture
def file_server():
    server = FileServer(bytes(range(256)) * 16000)
    yield server
    server.close()


@pytest.fixture
def manager(tmp_path):
    store = Store(tmp_path / "state")
    instance = DownloadManager(store, allow_private=True)
    instance.update_settings(SettingsPatch(download_dir=str(tmp_path / "downloads"), music_dir=str(tmp_path / "music")))
    yield instance
    instance.shutdown()


def wait_for(manager, task_id, status, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = manager.tasks.get(task_id)
        if task and task["status"] == status:
            return task
        if task and task["status"] == "error" and status != "error":
            raise AssertionError(task["error"])
        time.sleep(0.03)
    raise AssertionError(f"Timed out waiting for {status}: {manager.tasks.get(task_id)}")


@pytest.fixture(autouse=True)
def restore_proxy_environment():
    import os

    from ndm import network
    keys = network._ENV_KEYS
    saved = {key: os.environ.get(key) for key in keys}
    original = network._ORIGINAL_ENV
    yield
    network._ORIGINAL_ENV = original
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
