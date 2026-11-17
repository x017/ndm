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
    monkeypatch.setattr(extension, "host_manifest_dir", lambda browser="firefox": tmp_path / "hosts")
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
    source = (extension.extension_dir() / "core.js").read_text(encoding="utf-8")
    accept_check = source.index("if (!reply || !reply.ok)")
    cancel = source.index("api.cancel(item.id)")
    assert accept_check < cancel
    assert "return false;" in source[accept_check:cancel]


def test_installed_launcher_speaks_the_firefox_protocol(daemon, tmp_path, monkeypatch):
    """Spawn the real launcher and exchange length-prefixed frames with it."""
    import os
    import subprocess

    import ndm

    monkeypatch.setattr(extension, "host_manifest_dir", lambda browser="firefox": tmp_path / "hosts")
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
    monkeypatch.setattr(extension, "host_manifest_dir", lambda browser="firefox": tmp_path / "hosts")
    report = extension.doctor(tmp_path / "state")
    assert report["ok"] is False
    names = {c["name"]: c["ok"] for c in report["checks"]}
    assert names["host manifest"] is False
    assert names["launcher exists"] is False


def test_doctor_runs_the_launcher_and_surfaces_stderr(tmp_path, monkeypatch):
    """A launcher that cannot import ndm must be reported, not silently pass."""
    monkeypatch.setattr(extension, "host_manifest_dir", lambda browser="firefox": tmp_path / "hosts")
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

    monkeypatch.setattr(extension, "host_manifest_dir", lambda browser="firefox": tmp_path / "hosts")
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


def test_config_can_enable_the_handoff_without_the_gui(daemon, capsys):
    """The doctor tells users to run this, so it must work and be listed."""
    import ndm

    directory = str(daemon.data_dir)
    ndm.main(["config", "browser_integration", "true", "--data-dir", directory])
    assert "browser_integration = True" in capsys.readouterr().out
    assert request(daemon.data_dir, "state")["settings"]["browser_integration"] is True
    ndm.main(["config", "--list", "--data-dir", directory])
    listed = capsys.readouterr().out
    assert "browser_integration" in listed
    assert "browser_ask" in listed


def test_doctor_names_the_toggle_when_only_it_is_off(daemon, tmp_path, monkeypatch, capsys):
    """A correct install with the switch off must not say 're-run install'."""
    import os

    import ndm

    monkeypatch.setattr(extension, "host_manifest_dir", lambda browser="firefox": tmp_path / "hosts")
    extension.install(daemon.data_dir)
    src_root = str(Path(ndm.__file__).resolve().parent.parent)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(
        filter(None, [src_root, os.environ.get("PYTHONPATH", "")])))
    daemon.manager.update_settings(SettingsPatch(browser_integration=False))
    with pytest.raises(SystemExit) as exc:
        ndm.main(["extension", "doctor", "--data-dir", str(daemon.data_dir)])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Everything is installed correctly" in out
    assert "ndm config browser_integration true" in out
    assert "re-run: uv run ndm extension install" not in out


def test_cookie_browser_selects_the_extractor(manager):
    from ndm import network

    for choice in ("none", "firefox", "chrome", "chromium", "brave", "edge", "vivaldi"):
        settings = {"cookie_browser": choice}
        expected = {} if choice == "none" else {"cookiesfrombrowser": (choice,)}
        assert network.cookie_options(settings) == expected
        assert network.cookie_browser(settings) == choice
    # The legacy flag still selects Firefox.
    assert network.cookie_browser({"firefox_cookies": True}) == "firefox"
    assert network.cookie_options({"firefox_cookies": False}) == {}


def test_cookie_jar_uses_the_chosen_browser(manager, monkeypatch):
    import yt_dlp.cookies as cookies

    seen = []
    monkeypatch.setattr(cookies, "extract_cookies_from_browser",
                        lambda browser: seen.append(browser) or "jar")
    assert network_cookie_jar({"cookie_browser": "chromium"}) == "jar"
    assert seen == ["chromium"]
    assert network_cookie_jar({"cookie_browser": "none"}) is None
    assert seen == ["chromium"]


def network_cookie_jar(settings):
    from ndm.network import cookie_jar

    return cookie_jar(settings)


def test_cookie_error_names_the_browser_and_hides_profile_data(monkeypatch):
    import yt_dlp.cookies as cookies

    from ndm.network import cookie_jar

    def boom(browser):
        raise RuntimeError("/home/secret/profile/Cookies locked")

    monkeypatch.setattr(cookies, "extract_cookies_from_browser", boom)
    with pytest.raises(ValueError) as exc:
        cookie_jar({"cookie_browser": "chrome"})
    assert "Chrome" in str(exc.value)
    assert "/home/secret" not in str(exc.value)


def test_settings_keeps_the_legacy_firefox_flag_consistent():
    from ndm.models import Settings

    assert Settings.model_validate({"firefox_cookies": True}).cookie_browser == "firefox"
    assert Settings.model_validate({"cookie_browser": "chrome"}).firefox_cookies is False
    # An explicit "none" must win over a stale flag.
    off = Settings.model_validate({"cookie_browser": "none", "firefox_cookies": True})
    assert off.cookie_browser == "none" and off.firefox_cookies is False
    with pytest.raises(ValueError):
        Settings(cookie_browser="netscape")


def test_grab_offers_youtube_and_spotify_without_fetching(manager):
    yt = manager.offer_media(url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    sp = manager.offer_media(url="https://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl")
    assert manager.offers[yt["id"]]["kind"] == "youtube"
    assert manager.offers[yt["id"]]["category"] == "video"
    assert manager.offers[sp["id"]]["kind"] == "spotify"
    assert manager.offers[sp["id"]]["category"] == "audio"
    assert manager.tasks == {}
    with pytest.raises(ValueError):
        manager.offer_media(url="https://example.org/page.html")


def test_grab_routes_to_the_right_downloader(manager, monkeypatch):
    from unittest import mock

    import ndm.media as media

    yt = manager.offer_media(url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    with mock.patch.object(media, "inspect_media", return_value={"title": "A Clip", "items": []}), \
         mock.patch.object(manager, "add_media", return_value={"id": "yt1"}) as add_media:
        result = manager.resolve_offer(yt["id"], True)
    assert result["task"] == "yt1"
    assert add_media.call_args.kwargs["name"] == "A Clip"

    sp = manager.offer_media(url="https://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl")
    listing = {"items": [{"song": {"url": "https://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl"}}]}
    with mock.patch("ndm.spotify.search", return_value=listing), \
         mock.patch.object(manager, "add_spotify", return_value={"id": "sp1"}) as add_spotify:
        result = manager.resolve_offer(sp["id"], True)
    assert result["tasks"] == ["sp1"]
    assert add_spotify.called


def test_failed_grab_keeps_the_offer(manager, monkeypatch):
    from unittest import mock

    import ndm.media as media

    offer = manager.offer_media(url="https://youtu.be/dQw4w9WgXcQ")
    with mock.patch.object(media, "inspect_media", side_effect=ValueError("offline")):
        with pytest.raises(ValueError):
            manager.resolve_offer(offer["id"], True)
    assert offer["id"] in manager.offers


def test_host_forwards_a_grab(daemon):
    reply = host.handle({"type": "grab", "id": "g1",
                         "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}, daemon.data_dir)
    assert reply["ok"] is True and reply["status"] == "pending"
    assert request(daemon.data_dir, "state")["offers"][0]["kind"] == "youtube"


def test_chrome_manifest_id_matches_the_registered_origin():
    """Chrome derives the unpacked id from the manifest key; they must agree."""
    import base64
    import hashlib

    folder = extension.extension_dir("chrome")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    der = base64.b64decode(manifest["key"])
    digest = hashlib.sha256(der).hexdigest()[:32]
    derived = "".join(chr(ord("a") + int(c, 16)) for c in digest)
    assert derived == extension.CHROME_EXTENSION_ID
    assert "nativeMessaging" in manifest["permissions"]
    assert manifest["background"]["service_worker"] == "background.js"


def test_chrome_icons_are_png_not_svg():
    folder = extension.extension_dir("chrome")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    for relative in manifest["icons"].values():
        assert relative.endswith(".png")
        assert (folder / relative).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_shared_extension_files_do_not_drift():
    firefox = extension.extension_dir("firefox")
    chrome = extension.extension_dir("chrome")
    for name in ("core.js", "popup.html", "icon48.png", "icon128.png"):
        assert (firefox / name).read_bytes() == (chrome / name).read_bytes(), name


def test_chrome_worker_reads_settings_per_event():
    """A service worker can be torn down, so it must not cache settings."""
    source = (extension.extension_dir("chrome") / "background.js").read_text(encoding="utf-8")
    assert "importScripts(\"core.js\")" in source
    assert "async function currentSettings()" in source
    listener = source[source.index("downloads.onCreated"):]
    assert "await currentSettings()" in listener.split("});")[0]


@pytest.mark.parametrize("browser", ["chrome", "chromium", "brave", "edge"])
def test_chromium_hosts_use_allowed_origins(tmp_path, monkeypatch, browser):
    monkeypatch.setattr(extension, "host_manifest_dir",
                        lambda which=browser: tmp_path / which)
    info = extension.install(tmp_path / "state", browser)
    manifest = json.loads(Path(info["manifest"]).read_text(encoding="utf-8"))
    assert "allowed_extensions" not in manifest
    assert manifest["allowed_origins"] == [f"chrome-extension://{extension.CHROME_EXTENSION_ID}/"]
    assert info["extension"].endswith("extension/chrome")


def test_page_kind_detection_matches_the_engine():
    """The JS filter and the Python detector must agree on what is grabbable."""
    source = (extension.extension_dir("firefox") / "core.js").read_text(encoding="utf-8")
    assert "ndmPageKind" in source
    assert "youtu.be" in source and "open.spotify.com" in source
    from ndm.media import spotify_link, uses_ytdlp

    assert spotify_link("https://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl")
    assert uses_ytdlp("https://www.youtube.com/watch?v=dQw4w9WgXcQ")


def test_extension_runtime_with_browser_event_doubles():
    """Execute both shipped backgrounds, including MV3 worker restart."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for the extension event tests")
    script = Path(__file__).with_name("extension_runtime.test.cjs")
    result = subprocess.run([node, "--test", str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_python_and_javascript_page_classifiers_share_cases():
    import json

    from ndm.media import grab_page_kind

    cases = json.loads(Path(__file__).with_name("extension_page_cases.json").read_text())
    for url, expected in cases:
        assert grab_page_kind(url) == expected, url


def test_grab_kind_hint_cannot_override_the_url(manager):
    with pytest.raises(ValueError):
        manager.offer_media(url="https://example.org/page", kind="youtube")
    with pytest.raises(ValueError):
        manager.offer_media(url="https://www.youtube.com/watch?v=dQw4w9WgXcQ", kind="spotify")
    assert manager.offers == {}


def test_page_grabs_always_ask_even_when_file_asks_are_off(manager):
    manager.update_settings(SettingsPatch(browser_ask=False))
    result = manager.offer_media(url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert result["status"] == "pending"
    assert manager.tasks == {}


def test_page_grabs_expire_and_share_the_offer_cap(manager):
    old = manager.offer_media(url="https://youtu.be/dQw4w9WgXcQ")
    manager.offers[old["id"]]["created_at"] -= manager.OFFER_TTL + 1
    manager.offer_media(url="https://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl")
    assert old["id"] not in manager.offers
    for index in range(manager.OFFER_LIMIT - 1):
        manager.offer_download(url=f"https://example.org/{index}.mp4")
    with pytest.raises(ValueError):
        manager.offer_media(url="https://www.youtube.com/watch?v=aaaaaaaaaaa")


def test_youtube_grab_honours_add_paused(manager, monkeypatch):
    from unittest import mock

    import ndm.media as media

    manager.update_settings(SettingsPatch(engine_running=False))
    offer = manager.offer_media(url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    with mock.patch.object(media, "inspect_media", return_value={"title": "A clip"}):
        result = manager.resolve_offer(offer["id"], True, start_paused=True)
    assert manager.tasks[result["task"]]["status"] == "paused"
    assert manager.tasks[result["task"]]["source"] == "ytdlp"


def test_uninstalling_one_browser_keeps_the_shared_launcher(tmp_path, monkeypatch):
    monkeypatch.setattr(extension, "host_manifest_dir",
                        lambda browser: tmp_path / "hosts" / browser)
    firefox = extension.install(tmp_path / "state", "firefox")
    chrome = extension.install(tmp_path / "state", "chrome")
    assert firefox["launcher"] == chrome["launcher"]
    result = extension.uninstall(tmp_path / "state", "firefox")
    assert result["launcher_kept"] is True
    assert Path(chrome["launcher"]).is_file()
    assert extension.status(tmp_path / "state", "chrome")["installed"]
    result = extension.uninstall(tmp_path / "state", "chrome")
    assert result["launcher_kept"] is False
    assert not Path(chrome["launcher"]).exists()


def test_windows_registry_keys_are_vendor_specific():
    assert extension.windows_key_path("firefox") == r"Software\Mozilla\NativeMessagingHosts\ndm"
    assert extension.windows_key_path("chrome") == r"Software\Google\Chrome\NativeMessagingHosts\ndm"
    assert extension.windows_key_path("chromium") == r"Software\Chromium\NativeMessagingHosts\ndm"
    assert extension.windows_key_path("brave") == r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\ndm"
    assert extension.windows_key_path("edge") == r"Software\Microsoft\Edge\NativeMessagingHosts\ndm"


def test_unknown_browser_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        extension.install(tmp_path, "netscape")


def test_cli_browser_all_registers_every_vendor(tmp_path, monkeypatch, capsys):
    import ndm

    monkeypatch.setattr(extension, "host_manifest_dir",
                        lambda browser: tmp_path / "hosts" / browser)
    ndm.main(["extension", "install", "--browser", "all", "--data-dir", str(tmp_path / "state")])
    out = capsys.readouterr().out
    for browser in extension.BROWSERS:
        assert f"[{browser}] native host registered" in out
        assert (tmp_path / "hosts" / browser / "ndm.json").is_file()
    assert "extension/chrome" in out and "extension/firefox" in out
    ndm.main(["extension", "status", "--browser", "all", "--data-dir", str(tmp_path / "state")])
    out = capsys.readouterr().out
    for browser in extension.BROWSERS:
        assert f"Browser:           {browser}" in out


def test_cookie_settings_alias_matrix():
    from ndm.models import Settings

    cases = [
        ({}, "none", False),
        ({"firefox_cookies": True}, "firefox", True),
        ({"firefox_cookies": False}, "none", False),
        ({"cookie_browser": "chrome"}, "chrome", False),
        ({"cookie_browser": "chromium", "firefox_cookies": True}, "chromium", False),
        ({"cookie_browser": "none", "firefox_cookies": True}, "none", False),
        ({"cookie_browser": "firefox", "firefox_cookies": False}, "firefox", True),
    ]
    for raw, browser, legacy in cases:
        settings = Settings.model_validate(raw)
        assert settings.cookie_browser == browser
        assert settings.firefox_cookies is legacy
        assert Settings.model_validate(settings.model_dump()).model_dump() == settings.model_dump()


def test_chrome_cookies_reach_spotdl_matching_and_search_clients(monkeypatch):
    from http.cookiejar import CookieJar
    from unittest import mock

    from ndm import matching, spotify

    settings = {"cookie_browser": "chrome"}
    with mock.patch("ndm.network.cookie_jar", return_value=CookieJar()) as jar:
        client = matching.create_client(settings)
    jar.assert_called_once_with(settings)
    assert client._session is not None

    provider = mock.Mock()
    provider.audio_handler.params = {"cookiesfrombrowser": ("firefox",)}
    provider.client = None
    spotify._configure_audio_provider(provider, settings)
    assert provider.audio_handler.params["cookiesfrombrowser"] == ("chrome",)


def test_explicit_cookie_selector_beats_legacy_flag_in_raw_settings():
    from ndm.network import cookie_browser, cookie_options

    raw = {"cookie_browser": "none", "firefox_cookies": True}
    assert cookie_browser(raw) == "none"
    assert cookie_options(raw) == {}
    assert cookie_browser({"cookie_browser": "chrome", "firefox_cookies": True}) == "chrome"


def test_legacy_cookie_patch_can_change_an_existing_selector(manager):
    manager.update_settings(SettingsPatch(cookie_browser="chrome"))
    manager.update_settings(SettingsPatch(firefox_cookies=True))
    assert manager.settings.cookie_browser == "firefox"
    assert manager.settings.firefox_cookies is True
    manager.update_settings(SettingsPatch(firefox_cookies=False))
    assert manager.settings.cookie_browser == "none"
    assert manager.settings.firefox_cookies is False
