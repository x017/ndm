"""Firefox handoff: native host framing, daemon offers, and the in-app question."""
from __future__ import annotations

import io
import json
import struct
import threading
from pathlib import Path

import pytest

from ndm import browser_host as host
from ndm import extension
from ndm.daemon import NativeDaemon
from ndm.engine import DownloadManager
from ndm.ipc import request
from ndm.models import BrowserOffer, SettingsPatch
from ndm.store import Store


@pytest.fixture
def daemon(tmp_path):
    instance = NativeDaemon(tmp_path / "state")
    instance.manager.allow_private = True
    instance.manager.update_settings(SettingsPatch(
        download_dir=str(tmp_path / "dl"), browser_integration=True))
    thread = threading.Thread(target=instance.serve, daemon=True)
    thread.start()
    yield instance
    instance.stop_event.set()
    instance.close()
    thread.join(timeout=5)


@pytest.fixture
def manager(tmp_path):
    instance = DownloadManager(Store(tmp_path / "state"), allow_private=True)
    instance.update_settings(SettingsPatch(download_dir=str(tmp_path / "dl"),
                                           browser_integration=True))
    yield instance
    instance.shutdown()


def test_native_message_framing_round_trip():
    buffer = io.BytesIO()
    payload = {"type": "offer", "id": "dl-7", "url": "https://example.org/a.mp4"}
    host.write_message(payload, buffer)
    raw = buffer.getvalue()
    assert struct.unpack("=I", raw[:4])[0] == len(raw) - 4
    buffer.seek(0)
    assert host.read_message(buffer) == payload


def test_truncated_and_oversized_frames_are_rejected():
    assert host.read_message(io.BytesIO(b"")) is None
    assert host.read_message(io.BytesIO(b"\x05\x00")) is None
    # Header promises more than the body carries.
    assert host.read_message(io.BytesIO(struct.pack("=I", 50) + b"{}")) is None
    with pytest.raises(ValueError):
        host.read_message(io.BytesIO(struct.pack("=I", host.MAX_MESSAGE + 1)))


def test_offer_waits_instead_of_downloading(manager):
    result = manager.offer_download(url="https://example.org/clip.mp4",
                                    filename="clip.mp4", size=2048, mime="video/mp4")
    assert result["status"] == "pending"
    assert manager.tasks == {}
    offers = manager.snapshot()["offers"]
    assert len(offers) == 1
    assert offers[0]["name"] == "clip.mp4"
    assert offers[0]["category"] == "video"
    assert offers[0]["host"] == "example.org"


def test_declining_creates_no_task(manager):
    offer = manager.offer_download(url="https://example.org/no.mp3")
    assert manager.resolve_offer(offer["id"], False)["status"] == "declined"
    assert manager.tasks == {}
    assert manager.snapshot()["offers"] == []
    with pytest.raises(KeyError):
        manager.resolve_offer(offer["id"], True)


def test_accepting_queues_the_download(manager):
    offer = manager.offer_download(url="https://example.org/yes.mp3", filename="yes.mp3")
    result = manager.resolve_offer(offer["id"], True, start_paused=True)
    task = manager.tasks[result["task"]]
    assert task["source"] == "firefox"
    assert task["status"] == "paused"
    assert manager.snapshot()["offers"] == []


def test_duplicate_offers_collapse(manager):
    first = manager.offer_download(url="https://example.org/once.mp4")
    second = manager.offer_download(url="https://example.org/once.mp4")
    assert second["id"] == first["id"]
    assert second["duplicate"] is True
    assert len(manager.snapshot()["offers"]) == 1


def test_disabled_integration_refuses_so_firefox_keeps_the_file(manager):
    manager.update_settings(SettingsPatch(browser_integration=False))
    with pytest.raises(RuntimeError):
        manager.offer_download(url="https://example.org/a.mp4")
    assert manager.snapshot()["offers"] == []


def test_ask_off_starts_immediately(manager):
    manager.update_settings(SettingsPatch(browser_ask=False))
    result = manager.offer_download(url="https://example.org/auto.mp3")
    assert result["status"] == "started"
    assert manager.snapshot()["offers"] == []
    assert manager.tasks[result["task"]]["source"] == "firefox"


def test_offers_expire_and_are_capped(manager):
    offer = manager.offer_download(url="https://example.org/old.mp4")
    manager.offers[offer["id"]]["created_at"] -= manager.OFFER_TTL + 1
    manager.offer_download(url="https://example.org/new.mp4")
    assert offer["id"] not in manager.offers
    manager.update_settings(SettingsPatch(browser_ask=True))
    # One offer ("new.mp4") is already pending.
    for index in range(manager.OFFER_LIMIT - 1):
        manager.offer_download(url=f"https://example.org/f{index}.mp4")
    assert len(manager.offers) == manager.OFFER_LIMIT
    with pytest.raises(ValueError):
        manager.offer_download(url="https://example.org/overflow.mp4")


def test_private_targets_are_still_blocked(tmp_path):
    instance = DownloadManager(Store(tmp_path / "state"), allow_private=False)
    try:
        instance.update_settings(SettingsPatch(download_dir=str(tmp_path / "dl"),
                                               browser_integration=True))
        with pytest.raises(ValueError):
            instance.offer_download(url="http://127.0.0.1/secret.mp4")
    finally:
        instance.shutdown()


def test_host_forwards_an_offer_over_real_ipc(daemon):
    reply = host.handle({"type": "offer", "id": "dl-1", "url": "https://example.org/host.mp4",
                         "filename": "host.mp4", "size": 10}, daemon.data_dir)
    assert reply["ok"] is True
    assert reply["status"] == "pending"
    state = request(daemon.data_dir, "state")
    assert state["offers"][0]["name"] == "host.mp4"
    assert state["stats"]["total"] == 0


def test_host_ping_reports_integration_state(daemon):
    reply = host.handle({"type": "ping"}, daemon.data_dir)
    assert reply["ok"] and reply["running"] and reply["enabled"] and reply["ask"]


def test_host_refuses_without_crashing_when_daemon_is_absent(tmp_path):
    reply = host.handle({"type": "offer", "url": "https://example.org/x.mp4"}, tmp_path / "missing")
    assert reply["ok"] is False
    assert reply["error"]


def test_host_rejects_unknown_and_malformed_requests(tmp_path):
    assert host.handle({"type": "nope"}, tmp_path)["ok"] is False
    assert host.handle([], tmp_path)["ok"] is False


def test_browser_offer_model_bounds_input():
    with pytest.raises(ValueError):
        BrowserOffer(url="")
    with pytest.raises(ValueError):
        BrowserOffer(url="https://example.org/a", size=-1)
    with pytest.raises(ValueError):
        BrowserOffer(url="https://example.org/a", surprise=1)


def test_install_writes_a_manifest_pointing_at_a_launcher(tmp_path, monkeypatch):
    monkeypatch.setattr(extension, "host_manifest_dir", lambda: tmp_path / "hosts")
    info = extension.install(tmp_path / "state")
    manifest = json.loads(Path(info["manifest"]).read_text(encoding="utf-8"))
    assert manifest["name"] == "ndm"
    assert manifest["type"] == "stdio"
    assert manifest["allowed_extensions"] == [host.EXTENSION_ID]
    launcher = Path(manifest["path"])
    assert launcher.is_file()
    assert "ndm.browser_host" in launcher.read_text(encoding="utf-8")
    status = extension.status(tmp_path / "state")
    assert status["installed"] and status["path_matches"]
    assert extension.uninstall(tmp_path / "state")["removed"] is True
    assert not Path(info["manifest"]).exists()


def test_shipped_extension_manifest_is_valid_and_matches_the_host():
    folder = extension.extension_dir()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["browser_specific_settings"]["gecko"]["id"] == host.EXTENSION_ID
    assert "nativeMessaging" in manifest["permissions"]
    assert "downloads" in manifest["permissions"]
    for script in manifest["background"]["scripts"]:
        assert (folder / script).is_file()
    assert (folder / manifest["browser_action"]["default_popup"]).is_file()


def test_extension_only_cancels_after_the_host_accepts():
    """The cancel must sit after the ok check, or a refused offer loses the file."""
    source = (extension.extension_dir() / "background.js").read_text(encoding="utf-8")
    accept_check = source.index("if (!reply || !reply.ok)")
    cancel = source.index("browser.downloads.cancel")
    assert accept_check < cancel
    assert "return false;" in source[accept_check:cancel]


def test_installed_launcher_speaks_the_firefox_protocol(daemon, tmp_path, monkeypatch):
    """Spawn the real launcher and exchange length-prefixed frames with it."""
    import os
    import subprocess

    import ndm

    monkeypatch.setattr(extension, "host_manifest_dir", lambda: tmp_path / "hosts")
    info = extension.install(daemon.data_dir)
    src_root = str(Path(ndm.__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [src_root, env.get("PYTHONPATH", "")]))
    process = subprocess.Popen([info["launcher"]], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    def exchange(message):
        host.write_message(message, process.stdin)
        return host.read_message(process.stdout)

    try:
        ping = exchange({"type": "ping"})
        assert ping["ok"] and ping["running"] and ping["enabled"]
        reply = exchange({"type": "offer", "id": "dl-9", "url": "https://example.org/real.mp4",
                          "filename": "real.mp4", "size": 1234})
        assert reply["ok"] is True
        assert reply["status"] == "pending"
        assert reply["id"] == "dl-9"
        state = request(daemon.data_dir, "state")
        assert [o["name"] for o in state["offers"]] == ["real.mp4"]
        assert state["stats"]["total"] == 0
        request(daemon.data_dir, "browser.resolve",
                {"id": state["offers"][0]["id"], "accept": True, "start_paused": True})
        state = request(daemon.data_dir, "state")
        assert state["stats"]["total"] == 1
        assert state["downloads"][0]["source"] == "firefox"
    finally:
        process.stdin.close()
        process.wait(timeout=15)
    # Stray stdout would break framing; stderr must stay quiet on the happy path.
    assert process.returncode == 0
    assert not process.stderr.read().decode().strip()


def test_launcher_pins_the_data_dir(tmp_path):
    """Firefox does not inherit NDM_HOME, so the path must be explicit."""
    launcher = extension.write_launcher(tmp_path / "custom-state")
    body = launcher.read_text(encoding="utf-8")
    assert "--data-dir" in body
    assert str((tmp_path / "custom-state").resolve()) in body


def test_launcher_keeps_the_venv_interpreter_not_its_target(tmp_path, monkeypatch):
    """A venv's bin/python is a symlink; resolving it loses site-packages.

    Regression: resolve() turned .venv/bin/python into /usr/bin/python3, which
    cannot import ndm, so the host died and Firefox said "not reachable".
    """
    import sys

    real = tmp_path / "base-python"
    real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    real.chmod(0o755)
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    link = venv_bin / "python"
    link.symlink_to(real)
    assert link.resolve() == real.resolve()
    monkeypatch.setattr(sys, "executable", str(link))
    launcher = extension.write_launcher(tmp_path / "state")
    body = launcher.read_text(encoding="utf-8")
    assert str(link) in body
    assert str(real) not in body


def test_doctor_reports_a_missing_host(tmp_path, monkeypatch):
    monkeypatch.setattr(extension, "host_manifest_dir", lambda: tmp_path / "hosts")
    report = extension.doctor(tmp_path / "state")
    assert report["ok"] is False
    names = {c["name"]: c["ok"] for c in report["checks"]}
    assert names["host manifest"] is False
    assert names["launcher exists"] is False


def test_doctor_runs_the_launcher_and_surfaces_stderr(tmp_path, monkeypatch):
    """A launcher that cannot import ndm must be reported, not silently pass."""
    monkeypatch.setattr(extension, "host_manifest_dir", lambda: tmp_path / "hosts")
    info = extension.install(tmp_path / "state")
    broken = Path(info["launcher"])
    broken.write_text("#!/bin/sh\necho 'ModuleNotFoundError: No module named ndm' >&2\nexit 1\n",
                      encoding="utf-8")
    broken.chmod(0o755)
    report = extension.doctor(tmp_path / "state")
    assert report["ok"] is False
    failed = {c["name"]: c["detail"] for c in report["checks"] if not c["ok"]}
    assert "host stderr clean" in failed
    assert "ModuleNotFoundError" in failed["host stderr clean"]
    assert "ping ok" in failed


def test_doctor_passes_with_a_working_host(daemon, tmp_path, monkeypatch):
    import os

    import ndm

    monkeypatch.setattr(extension, "host_manifest_dir", lambda: tmp_path / "hosts")
    extension.install(daemon.data_dir)
    # The spawned host needs the same import path the tests use.
    src_root = str(Path(ndm.__file__).resolve().parent.parent)
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [src_root, existing])))
    report = extension.doctor(daemon.data_dir)
    assert report["reply"].get("ok") is True
    names = {c["name"]: c["ok"] for c in report["checks"]}
    assert names["ping ok"] and names["daemon running"]
    assert names["browser integration enabled"]
