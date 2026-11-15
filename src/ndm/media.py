"""Media discovery and yt-dlp integration. Protected Spotify streams are never extracted."""
from __future__ import annotations

import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
import yt_dlp

from .diagnostics import (
    YtdlpLogger,
    debug_enabled,
    failure_message,
    get_logger,
    log_failure,
    redact,
)
from .models import MEDIA_EXTENSIONS, category_for, safe_filename, validate_url
from .network import (
    FIREFOX_COOKIE_ERROR,
    firefox_cookie_jar,
    httpx_options,
    safe_error,
    ytdlp_options,
)

log = get_logger("ndm.media")

MEDIA_HOSTS = (
    "youtube.com", "youtu.be", "vimeo.com", "soundcloud.com", "bandcamp.com",
    "dailymotion.com", "twitch.tv", "archive.org",
)


def is_media_page(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in MEDIA_HOSTS)


def uses_ytdlp(url: str) -> bool:
    return is_media_page(url) or Path(urlsplit(url).path).suffix.lower() in {".m3u8", ".mpd"}


def spotify_link(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in {"spotify.com", "open.spotify.com", "play.spotify.com", "spotify.link"}


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "title":
            self._in_title = True
        key = "href" if tag == "a" else "src"
        if tag in {"a", "video", "audio", "source"} and values.get(key):
            self.links.append((values[key], values.get("title") or values.get("aria-label") or ""))
        if tag in {"video", "audio"} and values.get("poster"):
            self.links.append((values["poster"], "Video poster"))

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def _size_fields(size: int | float | None, *, estimated: bool = False) -> dict:
    try:
        value = max(0, int(size or 0))
    except (TypeError, ValueError, OverflowError):
        value = 0
    return {"size": value, "size_estimated": bool(value and estimated)}


def _probe_size(client: httpx.Client, url: str) -> dict:
    """Read a media length without downloading the body.

    HEAD is tried first; a one-byte ranged GET covers servers that reject HEAD
    or omit Content-Length. Accept-Encoding: identity keeps the reported length
    equal to the stored file size rather than a compressed transfer size.
    """
    try:
        response = client.head(url, headers={"Accept-Encoding": "identity"}, timeout=5)
        if response.status_code < 400:
            length = response.headers.get("content-length")
            if length and length.isdigit() and int(length) > 0:
                return _size_fields(int(length))
    except (httpx.HTTPError, ValueError):
        pass
    try:
        with client.stream("GET", url, headers={"Range": "bytes=0-0", "Accept-Encoding": "identity"}, timeout=5) as response:
            total = re.search(r"bytes \d+-\d+/(\d+)", response.headers.get("content-range", ""))
            if total:
                return _size_fields(int(total.group(1)))
            length = response.headers.get("content-length")
            if response.status_code == 200 and length and length.isdigit():
                return _size_fields(int(length))
    except (httpx.HTTPError, ValueError):
        pass
    return _size_fields(0)


def inspect_media(url: str, *, allow_private: bool = False, settings: dict | None = None) -> dict:
    settings = settings or {}
    resolve = not (settings.get("proxy_enabled") and settings.get("proxy_remote_dns", True))
    url = validate_url(url, allow_private=allow_private, resolve=resolve)
    if spotify_link(url):
        return {"kind": "spotify", "title": "Your Spotify link", "items": [],
                "message": "Use NDM's Spotify toolbar button to inspect this link with spotDL. It matches Spotify metadata to public audio sources; it does not extract Spotify's protected streams."}
    if uses_ytdlp(url):
        options = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True,
                   "socket_timeout": 15, "extractor_retries": 1, **ytdlp_options(settings)}
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.cookies.CookieLoadError as exc:
            # CookieLoadError is not a DownloadError. Keep profile/cookie details
            # out of IPC errors and give the user a way to recover.
            raise ValueError(FIREFOX_COOKIE_ERROR) from exc
        except yt_dlp.utils.DownloadError as exc:
            message = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))
            raise ValueError(safe_error(ValueError(message), settings)) from exc
        if info.get("is_live"):
            raise ValueError("Live streams are not supported. Try a finished recording.")
        items = []
        seen = set()
        for fmt in info.get("formats") or []:
            fid = fmt.get("format_id")
            if not fid or fmt.get("protocol") in {"mhtml", "f4m"}:
                continue
            has_video = fmt.get("vcodec") not in {None, "none"}
            has_audio = fmt.get("acodec") not in {None, "none"}
            if not has_video and not has_audio:
                continue
            height = fmt.get("height") or 0
            key = (height, has_audio, fmt.get("ext")) if has_video else (fmt.get("ext"), fmt.get("abr"))
            if key in seen:
                continue
            seen.add(key)
            exact = fmt.get("filesize")
            approx = fmt.get("filesize_approx")
            if not exact and not approx and info.get("duration") and fmt.get("tbr"):
                approx = float(info["duration"]) * float(fmt["tbr"]) * 125
            items.append({
                "id": str(fid), "url": url, "page_url": url,
                "name": safe_filename(info.get("title") or "Media"),
                "ext": fmt.get("ext") or "mp4", **_size_fields(exact or approx, estimated=not exact),
                "duration": float(info.get("duration") or 0),
                "quality": f"{height}p" if height else (f"{round(fmt.get('abr') or 0)} kbps" if has_audio else "Audio"),
                "category": "video" if has_video else "audio",
                "note": "Video + audio" if has_video and has_audio else ("Merges audio · FFmpeg required" if has_video else "Audio only"),
                "needs_ffmpeg": has_video and not has_audio,
            })
        items.sort(key=lambda i: (i["category"] == "video", i["size"]), reverse=True)
        if not items:
            items = [{"id": "best", "url": url, "page_url": url, "name": safe_filename(info.get("title") or "Media"),
                      "ext": info.get("ext") or "mp4",
                      **_size_fields(info.get("filesize") or info.get("filesize_approx"),
                                     estimated=not info.get("filesize")),
                      "duration": float(info.get("duration") or 0),
                      "quality": "Best available", "category": "video", "note": "Automatic format", "needs_ffmpeg": False}]
        return {"kind": "ytdlp", "title": info.get("title") or "Media", "items": items[:12],
                "duration": float(info.get("duration") or 0), "message": ""}
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in MEDIA_EXTENSIONS:
        with httpx.Client(follow_redirects=True, timeout=15, headers={"User-Agent": "NDM/0.1"},
                          cookies=firefox_cookie_jar(settings), **httpx_options(settings),
                          event_hooks={"request": [lambda request: validate_url(str(request.url), allow_private=allow_private, resolve=resolve)]}) as client:
            size = _probe_size(client, url)
        return {"kind": "direct", "title": safe_filename(urlsplit(url).path).rsplit(".", 1)[0],
                "items": [{"id": "original", "url": url, "name": urlsplit(url).path.rsplit("/", 1)[-1],
                           "ext": suffix[1:], **size, "quality": "Original", "category": category_for(url),
                           "note": "Direct media link", "needs_ffmpeg": False}], "message": ""}
    parser = LinkParser()
    size = 0
    try:
        with httpx.Client(follow_redirects=True, timeout=15, headers={"User-Agent": "NDM/0.1"},
                          cookies=firefox_cookie_jar(settings), **httpx_options(settings),
                          event_hooks={"request": [lambda request: validate_url(str(request.url), allow_private=allow_private, resolve=resolve)]}) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                mime = response.headers.get("content-type", "")
                if mime.startswith(("audio/", "video/")):
                    return {"kind": "direct", "title": "Direct media", "items": [
                        {"id": "original", "url": str(response.url), "name": response.url.path.rsplit("/", 1)[-1] or "media",
                         "ext": "mp4" if mime.startswith("video/") else "mp3",
                         **_size_fields(response.headers.get("content-length")),
                         "quality": "Original", "category": category_for("", mime), "note": mime, "needs_ffmpeg": False}], "message": ""}
                if "html" not in mime:
                    raise ValueError("No HTML page or playable media was found at this URL.")
                for chunk in response.iter_text():
                    size += len(chunk.encode())
                    if size > 2_000_000:
                        break
                    parser.feed(chunk)
    except httpx.HTTPError as exc:
        raise ValueError(f"Could not read this page: {safe_error(exc, settings)}") from exc
    items, seen = [], set()
    for link, title in parser.links:
        absolute = urljoin(url, link)
        if urlsplit(absolute).scheme not in {"http", "https"}:
            continue
        if Path(urlsplit(absolute).path).suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        try:
            absolute = validate_url(absolute, allow_private=allow_private, resolve=resolve)
        except ValueError:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        name = title or Path(urlsplit(absolute).path).name
        items.append({"id": str(len(items)), "url": absolute, "name": name,
                      "ext": Path(urlsplit(absolute).path).suffix[1:], **_size_fields(0),
                      "quality": "Original", "category": category_for(absolute),
                      "note": "Found on page", "needs_ffmpeg": False})
    items = items[:30]
    if items:
        with httpx.Client(follow_redirects=True, timeout=10, headers={"User-Agent": "NDM/0.1"},
                          cookies=firefox_cookie_jar(settings), **httpx_options(settings),
                          event_hooks={"request": [lambda request: validate_url(str(request.url), allow_private=allow_private, resolve=resolve)]}) as client:
            with ThreadPoolExecutor(max_workers=min(6, len(items))) as pool:
                sizes = list(pool.map(lambda item: _probe_size(client, item["url"]), items))
        for item, size in zip(items, sizes, strict=True):
            item.update(size)
    return {"kind": "html", "title": parser.title.strip() or "Page media", "items": items,
            "message": "Only direct media elements are detected. Scripts and protected streams are not bypassed." if items else "No direct audio or video links were found. Try a direct file URL."}


def quality_format(quality: str, ffmpeg: bool) -> str:
    if quality == "audio":
        return "bestaudio/best"
    height = {"1080": 1080, "720": 720}.get(quality)
    constraint = f"[height<={height}]" if height else ""
    if ffmpeg:
        return f"bestvideo{constraint}+bestaudio/best{constraint}/best"
    return f"best{constraint}[acodec!=none]/best[acodec!=none]"


def media_download(task: dict, stop, progress, rate_limit: int, connections: int, *, settings: dict | None = None) -> Path:
    """Run yt-dlp with controlled output, fragment concurrency and cooperative cancellation."""
    destination = Path(task["path"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = bool(shutil.which("ffmpeg"))

    def hook(data: dict) -> None:
        if stop.is_set():
            raise yt_dlp.utils.DownloadCancelled()
        total = data.get("total_bytes")
        estimated = False
        if not total:
            total = data.get("total_bytes_estimate") or 0
            estimated = bool(total)
        progress(int(data.get("downloaded_bytes") or 0), int(total), "downloading", estimated=estimated)

    def post_hook(data: dict) -> None:
        if stop.is_set():
            raise yt_dlp.utils.DownloadCancelled()
        if data.get("status") == "started":
            progress(None, None, "processing")

    # A logger object makes yt-dlp bypass quiet/no_warnings, so the network and
    # extractor warnings that explain a failure are captured instead of dropped.
    logger = YtdlpLogger(context=f"[{task.get('source') or 'media'}]")
    log.debug("Transfer start url=%s destination=%s proxy=%s cookies=%s",
              redact(task["url"]), destination, bool((settings or {}).get("proxy_enabled")),
              bool((settings or {}).get("firefox_cookies")))
    options = {
        "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
        "continuedl": True, "overwrites": False, "retries": 3, "fragment_retries": 3,
        "socket_timeout": 20, "concurrent_fragment_downloads": connections,
        "outtmpl": str(destination.with_suffix("")) + ".%(ext)s",
        "format": task.get("media_format") or quality_format(task.get("media_quality", "best"), ffmpeg),
        "progress_hooks": [hook], "postprocessor_hooks": [post_hook],
        # No "paths" home: outtmpl is already absolute, and yt-dlp warns that
        # --paths is ignored, which would add noise to every diagnostics log.
        "logger": logger, "verbose": debug_enabled(),
        **ytdlp_options(settings or {}),
    }
    if rate_limit:
        options["ratelimit"] = rate_limit
    spotdl_audio = task.get("source") == "spotdl"
    convert_mp3 = task.get("media_quality") == "audio"
    expected_ext = task.get("audio_ext", "mp3") if spotdl_audio else ("mp3" if convert_mp3 else None)
    if spotdl_audio or convert_mp3:
        if not ffmpeg:
            raise ValueError("Audio conversion needs FFmpeg on PATH. Install FFmpeg, then retry.")
        options["format"] = "bestaudio/best"
        options["final_ext"] = expected_ext
        bitrate = str(task.get("audio_bitrate") or "192k") if spotdl_audio else "192k"
        quality = None if bitrate in {"auto", "disable"} else bitrate.removesuffix("k")
        # yt-dlp names the Ogg/Vorbis encoder "vorbis"; the saved extension is still .ogg.
        codec = "vorbis" if expected_ext == "ogg" else expected_ext
        options["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": codec,
                                      "preferredquality": quality}]
    elif ffmpeg:
        options["merge_output_format"] = "mp4" if task["category"] == "video" else task.get("audio_ext", "m4a")
    if task.get("js_runtimes"):
        options["js_runtimes"] = task["js_runtimes"]
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(task["url"], download=True)
            if stop.is_set():
                raise yt_dlp.utils.DownloadCancelled()
            downloads = info.get("requested_downloads") or []
            for item in downloads:
                candidate = Path(item.get("filepath") or "")
                if candidate.is_file() and candidate.suffix != ".part":
                    if expected_ext:
                        converted = candidate.with_suffix("." + expected_ext)
                        if converted.is_file():
                            return converted
                    return candidate
            if expected_ext:
                converted = destination.with_suffix("." + expected_ext)
                if converted.is_file():
                    return converted
            candidates = [p for p in destination.parent.glob(destination.stem + ".*")
                          if p.is_file() and p.suffix not in {".part", ".ytdl", ".json"}]
            if not candidates:
                raise OSError("The extractor finished without creating a media file.")
            return max(candidates, key=lambda p: p.stat().st_mtime)
    except yt_dlp.utils.DownloadCancelled:
        raise
    except Exception as exc:
        if stop.is_set():
            raise yt_dlp.utils.DownloadCancelled() from exc
        log_failure(log, "media transfer", exc, settings=settings,
                    url=task["url"], source=task.get("source"))
        raise ValueError(failure_message(exc, detail=logger.summary(), settings=settings)) from exc
