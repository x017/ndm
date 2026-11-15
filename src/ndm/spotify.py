"""spotDL integration: Spotify metadata, matched public audio sources, local files.

This is the upstream spotDL Python API, not a Spotify protected-stream extractor.
No spotDL web server is started. Downloads are queued and owned by NDM's daemon.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import re
import shutil
import threading
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

from .diagnostics import (
    YtdlpLogger,
    configure_upstream_logging,
    debug_enabled,
    failure_message,
    get_logger,
    log_failure,
    redact,
)
from .models import SpotifyCancelled, safe_filename
from .network import (
    configure_environment,
    firefox_cookie_jar,
    firefox_cookie_options,
    httpx_options,
    proxy_url,
    requests_proxies,
)

log = get_logger("ndm.spotify")

_INIT_LOCK = threading.RLock()
_CLIENT_INITIALIZED = False


def supported_url(value: str) -> bool:
    parsed = urlsplit(value.strip())
    return (parsed.scheme in {"http", "https"} and parsed.hostname in {"open.spotify.com", "play.spotify.com"}
            and bool(re.match(r"^/(?:intl-[a-z]{2}/)?(?:track|album|playlist)/[A-Za-z0-9]{22}(?:/|$)", parsed.path)))


def availability() -> dict:
    try:
        # Inspect package metadata without importing spotdl's console/web entry
        # point. Only its metadata/matching modules are imported when requested.
        version = importlib.metadata.version("spotdl") if importlib.util.find_spec("spotdl") else None
    except (ImportError, importlib.metadata.PackageNotFoundError):
        version = None
    return {"installed": version is not None, "version": version, "ffmpeg": bool(shutil.which("ffmpeg")),
            "source": "YouTube Music (spotDL match)", "protected_spotify_audio": False}


def _configure_proxy(settings: dict | None) -> None:
    """Wire proxy into spotDL's separate metadata/audio clients.

    spotDL 4.x validates its own proxy setting with an HTTP/IPv4-only regex.
    Leave that setting empty and configure the underlying clients after init;
    this preserves hostname, SOCKS5, remote-DNS, and special-character support.
    """
    configure_environment(settings or {})
    proxies = requests_proxies(settings or {})
    from spotdl.utils.config import GlobalConfig
    GlobalConfig.set_parameter("proxies", proxies)
    with _INIT_LOCK:
        if _CLIENT_INITIALIZED:
            from spotdl.utils.spotify import SpotifyClient
            client = SpotifyClient()
            session = getattr(client, "_session", None)
            if session is not None:
                session.proxies = dict(proxies or {})
                session.trust_env = not proxies


def _configure_audio_provider(provider, settings: dict | None) -> None:
    data = settings or {}
    url = proxy_url(data)
    proxies = requests_proxies(data)
    if url:
        provider.audio_handler.params["proxy"] = url
    cookie_options = firefox_cookie_options(data)
    if cookie_options:
        provider.audio_handler.params.update(cookie_options)
    else:
        provider.audio_handler.params.pop("cookiesfrombrowser", None)
    # Upstream retries construct a fresh YTMusic client. Always route those through
    # NDM's configured session so proxy, opt-in cookies, and a timeout are preserved.
    provider._create_client = lambda: _configured_ytmusic(data)
    client = getattr(provider, "client", None)
    if client is not None:
        if hasattr(client, "proxies"):
            client.proxies = dict(proxies or {})
        provider.client = provider._create_client()


def _configured_ytmusic(settings: dict):
    import requests
    from ytmusicapi import YTMusic

    jar = firefox_cookie_jar(settings)
    proxies = requests_proxies(settings)
    session = requests.Session()
    session.request = partial(session.request, timeout=30)
    session.trust_env = not proxies
    if jar is not None:
        # Requests applies domain/path/secure matching to session cookies. Do not
        # flatten the jar into a header, which would leak cookies across hosts.
        session.cookies.update(jar)
    return YTMusic(language="de", proxies=proxies, requests_session=session)


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


def _resolve_spotify_url(url: str, settings: dict | None) -> str:
    value = url.strip()
    if urlsplit(value).hostname == "spotify.link":
        import httpx
        with httpx.Client(timeout=20, follow_redirects=True, **httpx_options(settings or {})) as client:
            value = str(client.head(value).url)
    if not supported_url(value):
        raise ValueError("Paste a full Spotify track, album, or playlist link from open.spotify.com.")
    return value


def _song_item(song) -> dict:
    return {"id": song.song_id or song.url, "name": song.name or "Unknown title",
            "artist": song.artist or ", ".join(song.artists or []),
            "album": song.album_name or "", "duration": int(song.duration or 0),
            "url": song.url, "song": song.json}


def _first_artist_name(artists) -> str | None:
    for artist in artists or []:
        if isinstance(artist, dict) and artist.get("name"):
            return artist["name"]
    return None


def _playlist_song(raw, *, list_name: str, list_url: str, list_position: int, list_length: int):
    """Convert one playlist page entry into a spotDL Song without extra per-track requests."""
    from spotdl.types.song import Song
    if not isinstance(raw, dict):
        return None
    track = raw.get("track") or raw.get("item")
    if not isinstance(track, dict) or track.get("is_local") or track.get("type") != "track":
        return None
    duration_ms = track.get("duration_ms") or 0
    if not track.get("id") or duration_ms <= 0:
        return None
    album = track.get("album") or {}
    artists = [a.get("name") for a in track.get("artists") or [] if isinstance(a, dict) and a.get("name")]
    release_date = album.get("release_date") or ""
    return Song.from_missing_data(
        name=track.get("name"), artists=artists, artist=artists[0] if artists else None,
        album_id=album.get("id"), album_name=album.get("name"),
        album_artist=_first_artist_name(album.get("artists")), album_type=album.get("album_type"),
        disc_number=track.get("disc_number"), duration=int(duration_ms / 1000),
        year=release_date[:4] if release_date else None, date=release_date or None,
        track_number=track.get("track_number"), tracks_count=album.get("total_tracks"),
        song_id=track.get("id"), explicit=track.get("explicit"),
        url=(track.get("external_urls") or {}).get("spotify"),
        isrc=(track.get("external_ids") or {}).get("isrc"),
        list_name=list_name, list_url=list_url, list_position=list_position, list_length=list_length,
    )


def _album_song(track, album: dict, *, list_position: int, list_length: int):
    from spotdl.types.song import Song
    if not isinstance(track, dict) or track.get("is_local"):
        return None
    duration_ms = track.get("duration_ms") or 0
    if not track.get("id") or duration_ms <= 0:
        return None
    artists = [a.get("name") for a in track.get("artists") or [] if isinstance(a, dict) and a.get("name")]
    release_date = album.get("release_date") or ""
    return Song.from_missing_data(
        name=track.get("name"), artists=artists, artist=artists[0] if artists else None,
        album_id=album.get("id"), album_name=album.get("name"),
        album_artist=_first_artist_name(album.get("artists")), album_type=album.get("album_type"),
        disc_number=track.get("disc_number"), duration=int(duration_ms / 1000),
        year=release_date[:4] if release_date else None, date=release_date or None,
        track_number=track.get("track_number"), tracks_count=album.get("total_tracks"),
        song_id=track.get("id"), explicit=track.get("explicit"), publisher=album.get("label"),
        url=(track.get("external_urls") or {}).get("spotify"),
        list_name=album.get("name"), list_url=(album.get("external_urls") or {}).get("spotify"),
        list_position=list_position, list_length=list_length,
    )


def search(url: str, data_dir: Path, *, limit: int = 500, settings: dict | None = None) -> dict:
    if urlsplit(url.strip()).hostname == "spotify.link":
        import httpx
        with httpx.Client(timeout=20, follow_redirects=True, **httpx_options(settings or {})) as client:
            url = str(client.head(url.strip()).url)
    if not supported_url(url):
        raise ValueError("Paste a full Spotify track, album, or playlist link from open.spotify.com.")
    if availability()["installed"] is False:
        raise RuntimeError("spotDL is not installed. Run uv sync, or pip install -e ., to install NDM's dependencies.")
    configure_upstream_logging()
    _initialize_client(data_dir)
    _configure_proxy(settings)
    from spotdl.utils import search as upstream_search
    from spotdl.utils.search import parse_query
    if proxy_url(settings or {}) or (settings or {}).get("firefox_cookies"):
        upstream_search.client = _configured_ytmusic(settings or {})
    else:
        upstream_search.client = None

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
    path = urlsplit(url).path
    kind = "playlist" if "/playlist/" in path else "album" if "/album/" in path else "track"
    first = songs[0]
    title = getattr(first, "list_name", None) or (first.album_name if kind == "album" else first.name if kind == "track" else None)
    return {"items": items, "truncated": truncated, "total": len(songs), "kind": kind, "title": title,
            "message": "spotDL matches these tracks to YouTube Music. The result is not Spotify's protected audio and may differ from the original recording."}


def search_stream(url: str, data_dir: Path, *, limit: int = 500,
                  settings: dict | None = None) -> Iterator[dict]:
    """Yield Spotify metadata as soon as each list page arrives.

    The desktop renders these events live, so titles/artists/albums appear while
    the rest of a long playlist is still being read. spotDL still selects the
    match; NDM transfers it. Missing per-track metadata is reinitialized before
    matching/tagging when the row is queued.
    """
    url = _resolve_spotify_url(url, settings)
    if availability()["installed"] is False:
        raise RuntimeError("spotDL is not installed. Run uv sync, or pip install -e ., to install NDM's dependencies.")
    configure_upstream_logging()
    _initialize_client(data_dir)
    _configure_proxy(settings)
    from spotdl.types.song import Song
    from spotdl.utils.spotify import SpotifyClient

    client = SpotifyClient()
    path = urlsplit(url).path
    kind = "playlist" if "/playlist/" in path else "album" if "/album/" in path else "track"
    if kind == "track":
        song = Song.from_url(url)
        yield {"type": "meta", "kind": kind, "title": song.name, "total": 1}
        yield {"type": "items", "items": [_song_item(song)]}
        yield {"type": "done", "total": 1, "truncated": False}
        return

    if kind == "playlist":
        playlist = client.playlist(url)
        if playlist is None:
            raise ValueError("Invalid playlist URL.")
        title = playlist.get("name") or ""
        total = int((playlist.get("tracks") or {}).get("total") or 0)
        yield {"type": "meta", "kind": kind, "title": title, "total": total}
        page = client.playlist_items(url)
        if page is None:
            raise ValueError(f"Wrong playlist id: {url}")
        convert = lambda raw, position: _playlist_song(  # noqa: E731
            raw, list_name=title, list_url=url, list_position=position, list_length=total or position)
    else:
        album = client.album(url)
        if album is None:
            raise ValueError("Couldn't get album metadata. Check that the album id is correct.")
        title = album.get("name") or ""
        total = int(album.get("total_tracks") or (album.get("tracks") or {}).get("total") or 0)
        yield {"type": "meta", "kind": kind, "title": title, "total": total}
        page = client.album_tracks(url)
        if page is None:
            raise ValueError("Couldn't get album metadata. Check that the album id is correct.")
        convert = lambda raw, position: _album_song(  # noqa: E731
            raw, album, list_position=position, list_length=total or position)

    position = 0
    count = 0
    truncated = False
    while page:
        batch = []
        for raw in page.get("items") or []:
            position += 1
            song = convert(raw, position)
            if song is None:
                continue
            if count >= limit:
                truncated = True
                break
            batch.append(_song_item(song))
            count += 1
        if batch:
            yield {"type": "items", "items": batch}
        if truncated or not page.get("next"):
            break
        page = client.next(page)
        if page is None:
            break
    if count == 0:
        raise ValueError("spotDL returned no tracks. Check that the link is public and available in your region.")
    yield {"type": "done", "total": max(total, count) if truncated else count,
           "truncated": truncated}


def estimated_size(song: dict, audio_format: str, bitrate: str) -> int:
    """Approximate final bytes for explicit lossy bitrates; lossless/auto stay unknown."""
    if audio_format == "flac" or bitrate in {"auto", "disable"}:
        return 0
    try:
        duration = float(song.get("duration") or 0)
        kilobits = int(str(bitrate).removesuffix("k"))
    except (TypeError, ValueError):
        return 0
    return int(duration * kilobits * 125) if duration > 0 and kilobits > 0 else 0


def match_song(song_data: dict, data_dir: Path, *, settings: dict | None = None, stop=None) -> dict:
    """Use spotDL only to select a public YouTube Music match for a Spotify track.

    The actual transfer is owned by NDM's media pipeline. spotDL may still inspect
    candidate metadata while ranking matches, but it never downloads the audio.
    """
    if availability()["installed"] is False:
        raise RuntimeError("spotDL is missing. Run uv sync, or pip install -e ., to reinstall NDM's dependencies.")
    configure_upstream_logging()
    _initialize_client(data_dir)
    _configure_proxy(settings)
    from spotdl.providers.audio import YouTubeMusic
    from spotdl.types.song import Song
    from spotdl.utils.search import reinit_song

    song = Song.from_missing_data(**song_data)
    if not song.name or not (song.artists or song.artist):
        raise ValueError("This Spotify track is missing the title/artist metadata needed for matching.")
    song.artists = list(song.artists or [song.artist])
    song.artist = song.artist or song.artists[0]
    song.duration = int(song.duration or 0)
    if stop is not None and stop.is_set():
        raise SpotifyCancelled()
    # Streamed playlist/album rows intentionally omit expensive per-track fields.
    # Fill them before matching/tagging, exactly as upstream's downloader did.
    if any(value is None for value in (song.genres, song.disc_count, song.tracks_count,
                                       song.track_number, song.album_id, song.album_artist)):
        # This refetches Spotify metadata over the network and can fail before
        # matching starts. Log it like any other stage; spotapi hides the real
        # cause in RequestError.error rather than __cause__.
        try:
            song = reinit_song(song)
        except Exception as exc:
            if stop is not None and stop.is_set():
                raise SpotifyCancelled() from exc
            log_failure(log, "Spotify metadata refresh", exc, track=song.display_name)
            raise ValueError(failure_message(
                exc, prefix=f"Could not refresh Spotify metadata for {song.display_name}",
            )) from exc
        song.artists = list(song.artists or [song.artist])
        song.artist = song.artist or (song.artists[0] if song.artists else "")
        song.duration = int(song.duration or 0)
    if stop is not None and stop.is_set():
        raise SpotifyCancelled()

    log.info("Matching %r by %r (duration=%ss isrc=%s) proxy=%s cookies=%s",
             song.name, song.artist, song.duration, song.isrc or "-",
             bool((settings or {}).get("proxy_enabled")), bool((settings or {}).get("firefox_cookies")))
    if (settings or {}).get("spotify_matcher") == "ndm":
        from .matching import find_match

        try:
            found = find_match(song.json, settings=settings, stop=stop)
        except (LookupError, SpotifyCancelled):
            raise
        except Exception as exc:
            log_failure(log, "YouTube Music matching (NDM)", exc, track=song.display_name)
            raise ValueError(failure_message(
                exc, prefix=f"YouTube Music matching failed for {song.display_name}")) from exc
        song.download_url = found["url"]
        log.info("Matched %r -> %s (score %.1f via %s)", song.display_name, found["url"],
                 found["score"], found["matched_by"])
        return {"url": found["url"], "song": song.json, "provider": "YouTube Music via NDM",
                "match_score": found["score"]}

    provider = YouTubeMusic()
    match_logger = YtdlpLogger("ndm.spotify.match", "[match]")
    try:
        _configure_audio_provider(provider, settings)
        # Capture spotDL's own yt-dlp candidate inspection instead of letting its
        # quiet/no_warnings settings discard the network detail.
        provider.audio_handler.params["logger"] = match_logger
        provider.audio_handler.params["verbose"] = debug_enabled()
        matched_url = provider.search(song, only_verified=False)
    except Exception as exc:
        # spotDL's view-count tie-breaker may inspect a candidate with yt-dlp and
        # wrap the real cause in AudioProviderError. Keep ranking unchanged, but
        # surface the underlying network/cookie/extractor failure to the queue.
        log_failure(log, "YouTube Music matching", exc, track=song.display_name)
        cause = exc.__cause__
        root = cause if cause is not None and cause is not exc else exc
        raise ValueError(failure_message(
            root, detail=match_logger.summary(),
            prefix=f"YouTube Music matching failed for {song.display_name}",
        )) from exc
    finally:
        provider.audio_handler.close()
    if stop is not None and stop.is_set():
        raise SpotifyCancelled()
    if not matched_url:
        log.error("No YouTube Music match for %r", song.display_name)
        raise LookupError(f"spotDL found no YouTube Music match for {song.display_name}. "
                          "Retry, or check the track title/artist. Full log: ndm logs")
    log.info("Matched %r -> %s", song.display_name, matched_url)
    song.download_url = matched_url
    return {"url": matched_url, "song": song.json, "provider": "YouTube Music via spotDL"}


def _taggable(song_data: dict) -> dict:
    """Fill fields spotDL writes unconditionally, because mutagen rejects None.

    Spotify legitimately omits ISRC/label/release date on some tracks, and
    streamed album/playlist rows omit even more. spotDL's embed_metadata assigns
    them straight into ID3/Vorbis/MP4 tags, so a None turns a finished download
    into "Invalid MultiSpec data: None" / "None needs to be str for key 'isrc'".
    """
    data = dict(song_data)
    for key in ("isrc", "date", "publisher", "url", "album_name"):
        if data.get(key) is None:
            data[key] = ""
    for key in ("disc_number", "disc_count", "track_number", "tracks_count"):
        if data.get(key) is None:
            data[key] = 0
    if not data.get("artists"):
        data["artists"] = [data.get("artist") or "Unknown artist"]
    if not data.get("artist"):
        data["artist"] = data["artists"][0]
    if not data.get("name"):
        data["name"] = "Unknown title"
    return data


def tag_file(path: Path, song_data: dict, *, stop=None, settings: dict | None = None) -> None:
    """Embed Spotify metadata/cover art after NDM has downloaded and converted audio.

    NDM writes the tags itself by default: only fields that actually have a
    value are written, so a track with no ISRC/label/date tags cleanly instead
    of failing after the audio was saved. "spotdl" keeps upstream's embedder.
    """
    if stop is not None and stop.is_set():
        raise SpotifyCancelled()

    writer = (settings or {}).get("spotify_tagger") or "ndm"
    log.info("Tagging %s with the %s writer", path, writer)
    try:
        if writer == "spotdl":
            from spotdl.types.song import Song
            from spotdl.utils.metadata import embed_metadata

            embed_metadata(path, Song.from_missing_data(**_taggable(song_data)))
        else:
            from .tagging import write_tags

            write_tags(path, song_data, settings=settings, stop=stop)
    except SpotifyCancelled:
        raise
    except Exception as exc:
        log_failure(log, "tagging", exc, path=str(path))
        cause = exc.__cause__ or exc
        detail = (redact(str(cause)) or type(cause).__name__)[:220]
        raise RuntimeError(
            f"Audio was saved to {path}, but tagging failed: {detail}. "
            "Retry this track to tag the saved file again. Full log: ndm logs"
        ) from exc
    if stop is not None and stop.is_set():
        raise SpotifyCancelled()


def download(song_data: dict, destination: Path, data_dir: Path, stop, progress, *,
             audio_format: str = "mp3", bitrate: str = "192k", settings: dict | None = None,
             rate_limit: int = 0, connections: int = 1, on_match=None) -> Path:
    """Match with spotDL, then transfer/convert through NDM's own yt-dlp pipeline."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is required to convert spotDL matches. Install FFmpeg, then retry this track.")
    if stop.is_set():
        raise SpotifyCancelled()
    progress(None, None, "matching", 0)
    matched = match_song(song_data, data_dir, settings=settings, stop=stop)
    if on_match is not None:
        on_match(matched)
    if stop.is_set():
        raise SpotifyCancelled()

    from spotdl.utils.deno import get_local_deno_yt_dlp_options

    from .media import media_download

    task = {
        "url": matched["url"], "path": str(destination), "category": "audio",
        "source": "spotdl", "media_format": "bestaudio/best",
        "audio_ext": audio_format, "audio_bitrate": bitrate,
        # Reuse a Deno binary the user already installed through spotDL. NDM does
        # not download or execute an installer implicitly.
        "js_runtimes": get_local_deno_yt_dlp_options().get("js_runtimes"),
    }

    def transfer_progress(downloaded, total, phase, *, estimated=False):
        if stop.is_set():
            raise SpotifyCancelled()
        progress(downloaded, total, phase, None, estimated=estimated)

    path = media_download(task, stop, transfer_progress, rate_limit, connections, settings=settings)
    if stop.is_set():
        raise SpotifyCancelled()
    progress(None, None, "tagging", 99)
    tag_file(path, matched["song"], stop=stop, settings=settings)
    if stop.is_set():
        raise SpotifyCancelled()
    return path


def display_name(song: dict, audio_format: str) -> str:
    title = safe_filename(song.get("name") or "Track")
    artist = safe_filename(song.get("artist") or "Unknown artist")
    return safe_filename(f"{artist} - {title}.{audio_format}")
