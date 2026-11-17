from __future__ import annotations

import time
from pathlib import Path

import pytest
from conftest import FileServer, wait_for

from ndm.engine import DownloadManager
from ndm.models import SettingsPatch
from ndm.store import Store


def test_segmented_download_integrity(manager, file_server):
    task = manager.add(url=file_server.url, filename="segmented.bin", connections=4)
    done = wait_for(manager, task["id"], "completed")
    assert Path(done["path"]).read_bytes() == file_server.payload
    assert done["downloaded"] == len(file_server.payload)
    assert len(done["segments"]) == 4
    assert not Path(done["part_path"]).exists()
    assert any(r and r != "bytes=0-0" for r in file_server.requests)


def test_non_range_download(manager):
    server = FileServer(b"no ranges, still a complete file" * 1000, ranges=False)
    try:
        task = manager.add(url=server.url, filename="plain.bin", connections=4)
        done = wait_for(manager, task["id"], "completed")
        assert Path(done["path"]).read_bytes() == server.payload
        assert not done["resumable"]
        assert len(done["segments"]) == 1
    finally:
        server.close()


def test_pause_and_resume_across_daemon_restart(tmp_path):
    server = FileServer(bytes(range(256)) * 32000, delay=0.012)
    state = tmp_path / "state"
    downloads = str(tmp_path / "downloads")
    try:
        store = Store(state)
        first = DownloadManager(store, allow_private=True)
        first.update_settings(SettingsPatch(download_dir=downloads, connections=4))
        task = first.add(url=server.url, filename="resume.bin")
        deadline = time.monotonic() + 8
        while first.tasks[task["id"]]["downloaded"] < 200000 and time.monotonic() < deadline:
            time.sleep(0.03)
        first.action(task["id"], "pause")
        deadline = time.monotonic() + 5
        while task["id"] in first.threads and time.monotonic() < deadline:
            time.sleep(0.03)
        partial = first.tasks[task["id"]]["downloaded"]
        assert 0 < partial < len(server.payload)
        first.shutdown()
        second = DownloadManager(Store(state), allow_private=True)
        try:
            assert second.tasks[task["id"]]["status"] == "paused"
            second.action(task["id"], "resume")
            done = wait_for(second, task["id"], "completed", timeout=20)
            assert Path(done["path"]).read_bytes() == server.payload
        finally:
            second.shutdown()
    finally:
        server.close()


def test_changed_validator_restarts_partial(tmp_path):
    server = FileServer(b"A" * 2_000_000, delay=0.01)
    state = tmp_path / "state"
    try:
        first = DownloadManager(Store(state), allow_private=True)
        first.update_settings(SettingsPatch(download_dir=str(tmp_path / "downloads")))
        task = first.add(url=server.url, filename="changed.bin")
        deadline = time.monotonic() + 5
        while first.tasks[task["id"]]["downloaded"] == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        first.action(task["id"], "pause")
        deadline = time.monotonic() + 5
        while task["id"] in first.threads and time.monotonic() < deadline:
            time.sleep(0.02)
        first.shutdown()
        server.payload = b"B" * 2_000_000
        server.etag = '"version-2"'
        second = DownloadManager(Store(state), allow_private=True)
        try:
            second.action(task["id"], "resume")
            done = wait_for(second, task["id"], "completed")
            assert Path(done["path"]).read_bytes() == server.payload
        finally:
            second.shutdown()
    finally:
        server.close()


def test_delete_cancels_and_cleans_partial(manager):
    server = FileServer(b"x" * 8_000_000, delay=0.02)
    try:
        task = manager.add(url=server.url, filename="delete.bin")
        time.sleep(0.3)
        manager.action(task["id"], "delete")
        deadline = time.monotonic() + 5
        while task["id"] in manager.threads and time.monotonic() < deadline:
            time.sleep(0.03)
        assert task["id"] not in manager.tasks
        assert not Path(task["part_path"]).exists()
    finally:
        server.close()


def test_completed_delete_keeps_file(manager, file_server):
    task = manager.add(url=file_server.url, filename="keep.bin")
    done = wait_for(manager, task["id"], "completed")
    result = manager.action(task["id"], "delete")
    assert result["file_kept"]
    assert Path(done["path"]).exists()


def test_duplicate_name_gets_unique_path(manager, file_server):
    first = manager.add(url=file_server.url, filename="same.bin")
    second = manager.add(url=file_server.url + "?copy=1", filename="same.bin")
    assert first["path"] != second["path"]
    wait_for(manager, first["id"], "completed")
    wait_for(manager, second["id"], "completed")


def test_engine_pause_stops_scheduling(manager, file_server):
    manager.update_settings(SettingsPatch(engine_running=False))
    task = manager.add(url=file_server.url, filename="later.bin")
    time.sleep(0.4)
    assert manager.tasks[task["id"]]["status"] == "queued"
    assert task["id"] not in manager.threads
    manager.update_settings(SettingsPatch(engine_running=True, resume_on_start=True))
    wait_for(manager, task["id"], "completed")


def test_media_adds_without_permission_checkbox(manager):
    task = manager.add(url="https://example.com/song.mp3", filename="song.mp3", start_paused=True)
    assert task["status"] == "paused"
    assert task["category"] == "audio"


def test_capture_is_opt_in_and_allowlisted(manager):
    with pytest.raises(RuntimeError, match="off"):
        manager.capture(url="https://example.com/a.mp4", filename=None, mime="video/mp4", source="clipboard")
    manager.update_settings(SettingsPatch(auto_capture=True, allowed_domains=["allowed.example"]))
    with pytest.raises(ValueError, match="allowlist"):
        manager.capture(url="https://other.example/a.mp4", filename=None, mime="video/mp4", source="clipboard")


def test_inbox_watches_link_files(manager, file_server):
    manager.update_settings(SettingsPatch(auto_capture=True, watch_inbox=True))
    inbox = manager.store.directory / "inbox"
    link = inbox / "links.txt"
    link.write_text(file_server.url.replace("file.bin", "clip.mp4") + "\n")
    deadline = time.monotonic() + 6
    while not (inbox / "links.txt.processed").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (inbox / "links.txt.processed").exists()
    task = next(t for t in manager.tasks.values() if t["source"] == "inbox")
    done = wait_for(manager, task["id"], "completed")
    assert Path(done["path"]).read_bytes() == file_server.payload


def test_store_rejects_second_owner(tmp_path):
    store = Store(tmp_path / "locked")
    try:
        with pytest.raises(RuntimeError, match="Another NDM daemon"):
            Store(tmp_path / "locked")
    finally:
        store.close()


def test_http_proxy_is_used_for_segmented_transfer(manager):
    proxy = FileServer(b"proxied payload" * 1000)
    try:
        manager.update_settings(SettingsPatch(proxy_enabled=True,
                                              proxy_url=f"http://127.0.0.1:{proxy.server.server_port}"))
        task = manager.add(url="http://remote.invalid/file.bin", filename="proxied.bin", connections=2)
        done = wait_for(manager, task["id"], "completed")
        assert Path(done["path"]).read_bytes() == proxy.payload
        assert proxy.requests
    finally:
        proxy.close()
