from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from ndm.daemon import NativeDaemon, is_running
from ndm.ipc import authentication_key, endpoint, request


@pytest.fixture
def daemon(tmp_path):
    instance = NativeDaemon(tmp_path / "state")
    thread = threading.Thread(target=instance.serve, daemon=True)
    thread.start()
    yield instance
    instance.stop_event.set()
    instance.close()
    thread.join(timeout=5)


def test_local_ipc_round_trip_and_settings(daemon):
    directory = daemon.data_dir
    assert is_running(directory)
    assert request(directory, "health")["transport"] == "local-ipc"
    assert request(directory, "state")["stats"]["total"] == 0
    request(directory, "settings", {"accent": "#169c83", "connections": 2})
    state = request(directory, "state")
    assert state["settings"]["accent"] == "#169c83"
    assert state["settings"]["connections"] == 2
    assert state["daemon"]["spotify"]["installed"]
    assert endpoint(directory)[1] == ("AF_PIPE" if __import__("os").name == "nt" else "AF_UNIX")


def test_invalid_settings_and_unknown_operations_are_rejected(daemon):
    with pytest.raises(ValueError):
        request(daemon.data_dir, "settings", {"connections": 99})
    assert request(daemon.data_dir, "state")["settings"]["connections"] == 4
    with pytest.raises(ValueError, match="Unknown"):
        request(daemon.data_dir, "execute", {"code": "anything"})


def test_spotify_link_uses_spotdl_queue(daemon):
    url = "https://open.spotify.com/track/" + "a" * 22
    with pytest.raises(ValueError, match="Spotify"):
        request(daemon.data_dir, "add", {"url": url})
    result = request(daemon.data_dir, "spotify.queue", {"songs": [{"url": url}], "start_paused": True})
    assert result["tasks"][0]["source"] == "spotdl"
    assert result["tasks"][0]["status"] == "paused"


def test_capture_is_opt_in(daemon):
    with pytest.raises(ValueError, match="off"):
        request(daemon.data_dir, "capture", {"url": "https://example.com/a.mp4"})


def test_ipc_key_is_private_and_wrong_key_rejected(daemon, monkeypatch):
    directory = daemon.data_dir
    key = authentication_key(directory)
    assert len(key) == 32
    if __import__("os").name != "nt":
        assert (directory / "ipc.key").stat().st_mode & 0o077 == 0
    monkeypatch.setattr("ndm.ipc.authentication_key", lambda _: b"x" * 32)
    with pytest.raises(RuntimeError, match="key changed"):
        request(directory, "health")


def test_download_through_native_daemon(daemon, file_server):
    from conftest import wait_for
    task = request(daemon.data_dir, "add", {"url": file_server.url, "filename": "native.bin"})
    done = wait_for(daemon.manager, task["id"], "completed")
    assert Path(done["path"]).read_bytes() == file_server.payload
    request(daemon.data_dir, "action", {"id": task["id"], "action": "delete"})
    assert Path(done["path"]).exists()
    assert not request(daemon.data_dir, "state")["downloads"]


def test_shutdown_stops_accepting_requests(daemon):
    request(daemon.data_dir, "shutdown")
    deadline = time.monotonic() + 3
    while daemon.accept_thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    daemon.close()
    assert not is_running(daemon.data_dir)


def test_old_persisted_settings_are_migrated_on_startup(tmp_path):
    from ndm.models import Settings
    from ndm.store import Store

    directory = tmp_path / "state"
    store = Store(directory)
    store.set_meta("settings", {"theme": "dark", "connections": 2})
    store.close()
    instance = NativeDaemon(directory)
    try:
        settings = request(directory, "state")["settings"]
        assert settings["theme"] == "dark"
        assert settings["connections"] == 2
        assert settings["proxy_enabled"] is False
        assert settings["proxy_url"] == ""
        assert settings["colors"] == {}
    finally:
        instance.close()
    store = Store(directory)
    try:
        saved = store.get_meta("settings", {})
    finally:
        store.close()
    assert saved == Settings.model_validate(saved).model_dump()
    assert saved["connections"] == 2
