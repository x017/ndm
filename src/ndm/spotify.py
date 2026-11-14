"""spotDL integration: Spotify metadata, matched public audio sources, local files.

This is the upstream spotDL Python API, not a Spotify protected-stream extractor.
No spotDL web server is started. Downloads are queued and owned by NDM's daemon.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import logging
import os
import re
import shutil
import threading
from pathlib import Path
from urllib.parse import urlsplit

from .models import safe_filename

_INIT_LOCK = threading.RLock()
_CLIENT_INITIALIZED = False


class SpotifyCancelled(Exception):
    pass


def supported_url(value: str) -> bool:
    parsed = urlsplit(value.strip())
    return (parsed.scheme in {"http", "https"} and parsed.hostname in {"open.spotify.com", "play.spotify.com"}
            and bool(re.match(r"^/(?:intl-[a-z]{2}/)?(?:track|album|playlist)/[A-Za-z0-9]{22}(?:/|$)", parsed.path)))


def availability() -> dict:
    try:
        # Inspect package metadata without importing spotdl's console/web entry
        # point. Only its search/downloader modules are imported when requested.
        version = importlib.metadata.version("spotdl") if importlib.util.find_spec("spotdl") else None
    except (ImportError, importlib.metadata.PackageNotFoundError):
        version = None
    return {"installed": version is not None, "version": version, "ffmpeg": bool(shutil.which("ffmpeg")),
            "source": "YouTube Music (spotDL match)", "protected_spotify_audio": False}


def _settings(data_dir: Path, output: Path, audio_format: str, bitrate: str) -> dict:
    return {
        "audio_providers": ["youtube-music"], "lyrics_providers": [],
        "output": str(output), "format": audio_format, "bitrate": bitrate,
        "threads": 1, "simple_tui": True, "load_config": False,
        "log_level": "ERROR", "overwrite": "force", "print_errors": False,
        "generate_lrc": False, "sponsor_block": False, "scan_for_songs": False,
        "only_verified_results": False, "fetch_albums": False,
        "max_filename_length": 180, "m3u": None, "archive": None,
        "save_file": None, "save_errors": None, "proxy": None,
    }


def _initialize_client(data_dir: Path) -> None:
    global _CLIENT_INITIALIZED
    with _INIT_LOCK:
        if _CLIENT_INITIALIZED:
            return
        from spotdl.utils.config import SPOTIFY_OPTIONS
        from spotdl.utils.spotify import SpotifyClient

        # Upstream's default metadata backend needs no browser login. Users may opt into
        # their own official API application with environment variables; no user_auth
        # callback server is opened by this desktop application.
        SpotifyClient.init(
            client_id=os.environ.get("SPOTIPY_CLIENT_ID", SPOTIFY_OPTIONS["client_id"]),
            client_secret=os.environ.get("SPOTIPY_CLIENT_SECRET", SPOTIFY_OPTIONS["client_secret"]),
            use_official_api=bool(os.environ.get("SPOTIPY_CLIENT_ID")),
            user_auth=False, headless=True, no_cache=False,
            cache_path=str(data_dir / "spotify-token-cache"),
        )
        _CLIENT_INITIALIZED = True


def search(url: str, data_dir: Path, *, limit: int = 500) -> dict:
    if not supported_url(url):
        raise ValueError("Paste a full Spotify track, album, or playlist link from open.spotify.com.")
    if availability()["installed"] is False:
        raise RuntimeError("spotDL is not installed. Run uv sync, or pip install -e ., to install NDM's dependencies.")
    logging.getLogger("spotdl").setLevel(logging.ERROR)
    _initialize_client(data_dir)
    from spotdl.utils.search import parse_query

    # parse_query is the same metadata expansion used by spotdl.Spotdl.search.
    songs = parse_query([url.strip()], threads=2)
    if not songs:
        raise ValueError("spotDL returned no tracks. Check that the link is public and available in your region.")
    truncated = len(songs) > limit
    items = []
    for song in songs[:limit]:
        data = song.json
        items.append({"id": song.song_id or song.url, "name": song.name or "Unknown title",
                      "artist": song.artist or ", ".join(song.artists or []),
                      "album": song.album_name or "", "duration": int(song.duration or 0),
                      "url": song.url, "song": data})
    return {"items": items, "truncated": truncated,
            "message": "spotDL matches these tracks to YouTube Music. The result is not Spotify's protected audio and may differ from the original recording."}


def download(song_data: dict, destination: Path, data_dir: Path, stop, progress, *,
             audio_format: str = "mp3", bitrate: str = "192k") -> Path:
    """Run upstream's single-song downloader with a cooperative native progress hook."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is required by spotDL. Install FFmpeg, then retry this track.")
    availability_info = availability()
    if not availability_info["installed"]:
        raise RuntimeError("spotDL is missing. Run uv sync, or pip install -e ., to reinstall NDM's dependencies.")
    logging.getLogger("spotdl").setLevel(logging.ERROR)
    _initialize_client(data_dir)
    import asyncio

    from spotdl.download.downloader import Downloader
    from spotdl.types.song import Song

    song = Song.from_missing_data(**song_data)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # A per-task directory prevents upstream's filename template from escaping NDM's
    # chosen destination or colliding with another queued track.
    template = str(destination.parent / (destination.stem + ".{output-ext}"))
    loop = asyncio.new_event_loop()
    # spotDL constructs its semaphore before entering run_until_complete; bind it
    # to this worker's loop explicitly (the daemon may have serviced earlier jobs).
    asyncio.set_event_loop(loop)
    downloader = Downloader(settings=_settings(data_dir, Path(template), audio_format, bitrate), loop=loop)

    def on_update(tracker, message):
        if stop.is_set():
            raise SpotifyCancelled()
        phase = "processing" if "convert" in message.lower() or "metadata" in message.lower() else "downloading"
        progress(None, None, phase, min(99, int(tracker.progress)))

    downloader.progress_handler.update_callback = on_update
    try:
        if stop.is_set():
            raise SpotifyCancelled()
        _, path = downloader.download_song(song)
        if stop.is_set():
            raise SpotifyCancelled()
        if path is None or not Path(path).is_file():
            detail = "; ".join(downloader.errors[-2:])
            raise ValueError(detail or "spotDL could not find or download a matching audio source.")
        return Path(path)
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()
            asyncio.set_event_loop(None)


def display_name(song: dict, audio_format: str) -> str:
    title = safe_filename(song.get("name") or "Track")
    artist = safe_filename(song.get("artist") or "Unknown artist")
    return safe_filename(f"{artist} - {title}.{audio_format}")
