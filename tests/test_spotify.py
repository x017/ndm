from __future__ import annotations

import threading
from pathlib import Path

import pytest

from ndm import spotify
from ndm.models import SettingsPatch

TRACK = "https://open.spotify.com/track/" + "a" * 22


def song_data():
    return {"url": TRACK, "name": "Own recording", "artist": "Local artist", "song_id": "a" * 22}


def test_spotify_url_validation():
    assert spotify.supported_url(TRACK)
    assert spotify.supported_url("https://open.spotify.com/intl-de/playlist/" + "b" * 22)
    assert not spotify.supported_url("https://spotify.link/short")
    assert not spotify.supported_url("https://open.spotify.com.evil.example/track/" + "a" * 22)
    assert not spotify.supported_url("https://open.spotify.com/artist/" + "a" * 22)


def test_queue_requires_permission_and_deduplicates(manager):
    with pytest.raises(ValueError, match="permission"):
        manager.add_spotify(song_data())
    first = manager.add_spotify(song_data(), authorized=True, start_paused=True)
    second = manager.add_spotify(song_data(), authorized=True)
    assert first["id"] == second["id"]
    assert first["source"] == "spotdl"
    assert first["provider"] == "YouTube Music via spotDL"
    assert first["status"] == "paused"
    assert first["category"] == "audio"
    assert first["spotify_format"] == "mp3"


def test_engine_uses_spotdl_adapter_and_keeps_queue_controls(manager, monkeypatch):
    from conftest import wait_for
    calls = []

    def fake_download(song, destination, data_dir, stop, progress, **options):
        calls.append((song, options))
        progress(None, None, "downloading", 40)
        destination.write_bytes(b"authorized local fixture")
        return destination

    monkeypatch.setattr(spotify, "download", fake_download)
    task = manager.add_spotify(song_data(), authorized=True)
    done = wait_for(manager, task["id"], "completed")
    assert Path(done["path"]).read_bytes() == b"authorized local fixture"
    assert done["percent"] == 100
    assert calls[0][1] == {"audio_format": "mp3", "bitrate": "192k"}
    manager.update_settings(SettingsPatch(engine_running=False))


def test_spotdl_failure_is_visible_and_retryable(manager, monkeypatch):
    from conftest import wait_for

    def fail(*args, **kwargs):
        raise ValueError("No matching source")

    monkeypatch.setattr(spotify, "download", fail)
    task = manager.add_spotify(song_data(), authorized=True)
    failed = wait_for(manager, task["id"], "error")
    assert failed["error"] == "No matching source"
    manager.action(task["id"], "retry")
    assert manager.tasks[task["id"]]["status"] in {"queued", "downloading", "error"}


def test_spotdl_cancellation_pauses_without_marking_complete(manager, monkeypatch):
    from conftest import wait_for
    started = threading.Event()

    def cancel(song, destination, data_dir, stop, progress, **options):
        started.set()
        while not stop.wait(0.02):
            pass
        raise spotify.SpotifyCancelled()

    monkeypatch.setattr(spotify, "download", cancel)
    task = manager.add_spotify(song_data(), authorized=True)
    assert started.wait(3)
    manager.action(task["id"], "pause")
    paused = wait_for(manager, task["id"], "paused")
    assert paused["status"] == "paused"
    assert not Path(paused["path"]).exists()


def test_search_uses_upstream_metadata_api_without_web_server(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace

    class Song:
        song_id = "a" * 22
        url = TRACK
        name = "Own recording"
        artist = "Local artist"
        artists = [artist]
        album_name = "Local album"
        duration = 123
        json = song_data()

    calls = []
    monkeypatch.setattr(spotify, "_initialize_client", lambda directory: None)
    monkeypatch.setitem(sys.modules, "spotdl.utils.search", SimpleNamespace(
        parse_query=lambda query, threads: calls.append((query, threads)) or [Song()]))
    result = spotify.search(TRACK, tmp_path)
    assert calls == [([TRACK], 2)]
    assert result["items"][0]["duration"] == 123
    assert "not Spotify's protected audio" in result["message"]


def test_real_spotdl_downloader_with_local_audio_fixture(monkeypatch, tmp_path):
    """Exercise upstream's actual async download pipeline without external services."""
    import shutil

    from spotdl.download import downloader as upstream
    from spotdl.providers.audio.base import AudioProvider
    from spotdl.types.song import Song
    from spotdl.utils import config

    cache = tmp_path / "spotdl-cache"
    cache.mkdir()
    source = tmp_path / "authorized.m4a"
    source.write_bytes(b"synthetic audio fixture, not a copyrighted recording")
    monkeypatch.setattr(spotify, "_initialize_client", lambda directory: None)
    monkeypatch.setattr(config, "get_spotdl_path", lambda: cache)
    monkeypatch.setattr(spotify.shutil, "which", lambda name: "/fixture/ffmpeg" if name == "ffmpeg" else shutil.which(name))
    monkeypatch.setattr(upstream.Downloader, "search", lambda self, song: "https://example.org/authorized")
    monkeypatch.setattr(upstream, "embed_metadata", lambda *args, **kwargs: None)

    def metadata(self, url, download=False):
        path = config.get_temp_path() / "fixture.m4a"
        shutil.copyfile(source, path)
        return {"id": "fixture", "ext": "m4a", "abr": 128}

    monkeypatch.setattr(AudioProvider, "get_download_metadata", metadata)
    destination = tmp_path / "downloads" / "Local artist - Own recording.m4a"
    song = Song.from_missing_data(**song_data(), artists=["Local artist"], genres=[], disc_count=1,
                                 tracks_count=1, track_number=1, album_id="local", album_artist="Local artist",
                                 explicit=False, duration=1).json
    updates = []
    result = spotify.download(song, destination, tmp_path, threading.Event(),
                              lambda *args: updates.append(args), audio_format="m4a", bitrate="disable")
    assert result == destination
    assert result.read_bytes() == source.read_bytes()
    assert updates[-1][3] == 99
    assert not (cache / "temp" / "fixture.m4a").exists()
    # A second job on the same daemon worker must get a fresh, correctly bound loop.
    second = spotify.download(song, destination, tmp_path, threading.Event(),
                              lambda *args: None, audio_format="m4a", bitrate="disable")
    assert second == destination


def test_real_spotdl_package_imports():
    info = spotify.availability()
    assert info["installed"]
    assert info["version"].startswith("4.")
    from spotdl.download.downloader import Downloader
    from spotdl.types.song import Song
    assert callable(Downloader)
    assert Song.from_missing_data(**song_data()).name == "Own recording"
