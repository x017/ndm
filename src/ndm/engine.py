"""Resumable, segmented HTTP download engine. All network work stays off the UI thread."""
from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from .diagnostics import Tracker, get_logger, redact
from .models import (
    MEDIA_EXTENSIONS,
    Settings,
    SettingsPatch,
    category_for,
    safe_filename,
    validate_url,
)
from .network import (
    FIREFOX_COOKIE_ERROR,
    configure_environment,
    firefox_cookie_jar,
    httpx_options,
    safe_error,
)
from .store import Store

log = get_logger("ndm.engine")

CHUNK = 128 * 1024
USER_AGENT = "NDM/0.1 (personal download manager)"
TERMINAL = {"completed", "error"}


class RangeLost(Exception):
    pass


class DownloadManager:
    def __init__(self, store: Store, *, allow_private: bool = False):
        self.store = store
        self.allow_private = allow_private
        self.lock = threading.RLock()
        saved_settings = store.get_meta("settings", {})
        self.settings = Settings.model_validate(saved_settings)
        # Persist defaults for fields introduced by newer versions immediately,
        # so an upgrade does not leave a partial settings object on disk.
        if self.settings.model_dump() != saved_settings:
            store.set_meta("settings", self.settings.model_dump())
        configure_environment(self.settings)
        self.tasks = store.load_downloads()
        self.stop = threading.Event()
        self.controls: dict[str, threading.Event] = {}
        self._deleted_paths: dict[str, list[str]] = {}
        self.reasons: dict[str, str] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.dirty: set[str] = set()
        self.started = time.time()
        self.speed_history: deque[float] = deque([0.0] * 60, maxlen=60)
        self.root = Path(self.settings.download_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.store.directory / "inbox").mkdir(exist_ok=True)
        for task in self.tasks.values():
            task["speed"] = 0
            if task["status"] in {"downloading", "queued", "pausing"}:
                task["status"] = "queued" if self.settings.resume_on_start else "paused"
                task["error"] = ""
        self.store.save_downloads(list(self.tasks), self.tasks)
        self.thread = threading.Thread(target=self._loop, name="ndm-scheduler", daemon=True)
        self.thread.start()

    def validate_target(self, url: str) -> str:
        # With remote DNS, the proxy resolves the target; do not leak a local DNS lookup.
        return validate_url(url, allow_private=self.allow_private,
                            resolve=not (self.settings.proxy_enabled and self.settings.proxy_remote_dns))

    def _mark(self, task_id: str) -> None:
        self.dirty.add(task_id)

    def _unique(self, directory: Path, name: str) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        candidate = directory / name
        reserved = {t["path"] for t in self.tasks.values() if t["status"] != "completed"}
        index = 1
        while candidate.exists() or str(candidate) in reserved:
            candidate = directory / f"{Path(name).stem} ({index}){Path(name).suffix}"
            index += 1
        return candidate

    def add(self, *, url: str, filename: str | None = None, category: str | None = None,
            connections: int | None = None, start_paused: bool = False,
            source: str = "manual", dedupe: bool = True) -> dict:
        from .media import spotify_link
        if spotify_link(url):
            raise ValueError("Use the Spotify toolbar button or ndm spotify command for Spotify links.")
        url = self.validate_target(url)
        parsed = urlsplit(url)
        name = safe_filename(filename or Path(unquote(parsed.path)).name or "download")
        kind = category or category_for(name)
        with self.lock:
            if dedupe:
                for task in self.tasks.values():
                    if task["url"] == url and task["status"] not in {"error", "completed"}:
                        return dict(task)
            directory = self.root / kind
            path = self._unique(directory, name)
            task_id = uuid.uuid4().hex[:12]
            task = {
                "id": task_id, "url": url, "name": path.name, "category": kind,
                "status": "paused" if start_paused else "queued", "size": 0,
                "downloaded": 0, "speed": 0, "connections": connections or self.settings.connections,
                "segments": [], "resumable": False, "etag": "", "last_modified": "",
                "mime": "", "error": "", "source": source, "size_estimated": False,
                "created_at": time.time(), "completed_at": None,
                "path": str(path), "part_path": str(path) + ".part",
                "host": parsed.hostname or "", "final_url": "", "explicit_name": bool(filename),
                "explicit_category": bool(category),
            }
            self.tasks[task_id] = task
            self.store.save_downloads([task_id], self.tasks)
            self.store.event("Added to your downloads", task["name"])
            log.info("[%s] QUEUED url=%s name=%r category=%s connections=%s paused=%s path=%s",
                     task_id, redact(url), task["name"], kind, task["connections"],
                     bool(start_paused), path)
            return dict(task)

    def add_media(self, *, url: str, format_id: str, name: str, category: str,
                  quality: str = "best", ext: str = "mp4", size: int = 0,
                  size_estimated: bool = False) -> dict:
        from .media import spotify_link, uses_ytdlp
        url = self.validate_target(url)
        if spotify_link(url):
            raise ValueError("Use the Spotify toolbar button or ndm spotify command to queue this link through spotDL.")
        convert_mp3 = quality == "audio"
        if convert_mp3:
            if not shutil.which("ffmpeg"):
                raise ValueError("MP3 conversion needs FFmpeg on PATH. Install FFmpeg, then retry.")
            category, ext, format_id = "audio", "mp3", "best"
        if not uses_ytdlp(url) and not convert_mp3:
            with self.lock:
                # A direct media download must not dedupe onto an MP3 conversion
                # of the same URL (source="ytdlp"); those are separate outputs.
                existing = next((t for t in self.tasks.values() if t["url"] == url
                                 and t.get("source") != "ytdlp"
                                 and t["status"] not in {"error", "completed"}), None)
                if existing:
                    return dict(existing)
                task = self.add(url=url, filename=name, category=category, source="media", dedupe=False)
                current = self.tasks[task["id"]]
                if size > 0:
                    current.update(size=size, size_estimated=size_estimated)
                self.store.save_downloads([task["id"]], self.tasks)
                return dict(current)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", format_id):
            raise ValueError("Invalid media format. Inspect the link again.")
        if category not in {"video", "audio"}:
            raise ValueError("Choose audio or video.")
        ext = safe_filename("file." + ext).rsplit(".", 1)[-1].lower()
        name = safe_filename(name, "media") + "." + ext
        with self.lock:
            existing = next((t for t in self.tasks.values() if t["url"] == url
                             and t.get("format_id") == format_id
                             and t.get("media_quality", "best") == quality
                             and t["status"] != "error"), None)
            if existing:
                return dict(existing)
            task = self.add(url=url, filename=name, category=category, source="ytdlp", dedupe=False)
            current = self.tasks[task["id"]]
            current["format_id"] = format_id
            current["media_quality"] = quality
            current["media_format"] = (f"{format_id}+bestaudio/best" if category == "video" else format_id) if format_id != "best" else None
            if convert_mp3:
                current["audio_ext"] = "mp3"
            if size > 0:
                current.update(size=size, size_estimated=size_estimated)
            self.store.save_downloads([task["id"]], self.tasks)
            return dict(current)

    def add_spotify(self, song: dict, *, start_paused: bool = False) -> dict:
        """Queue a spotDL metadata match. This never accesses Spotify protected audio."""
        from .spotify import display_name, estimated_size, supported_url

        url = str(song.get("url", ""))
        if not supported_url(url) or "/track/" not in url:
            raise ValueError("spotDL queue entries must be individual Spotify tracks.")
        with self.lock:
            existing = next((t for t in self.tasks.values() if t["url"] == url and t["status"] != "error"), None)
            if existing:
                log.info("QUEUE dedupe: %s already queued as [%s] (%s)", redact(url),
                         existing["id"], existing["status"])
                return dict(existing)
            name = display_name(song, self.settings.spotify_format)
            path = self._unique(self.root / "audio", name)
            task_id = uuid.uuid4().hex[:12]
            estimate = estimated_size(song, self.settings.spotify_format, self.settings.spotify_bitrate)
            task = {
                "id": task_id, "url": url, "name": name, "category": "audio",
                "status": "paused" if start_paused else "queued", "size": estimate,
                "downloaded": 0, "speed": 0, "connections": self.settings.connections, "segments": [],
                "resumable": False, "etag": "", "last_modified": "", "mime": "audio/mpeg",
                "error": "", "source": "spotdl", "size_estimated": bool(estimate),
                "created_at": time.time(), "completed_at": None,
                "path": str(path), "part_path": str(path) + ".part",
                "host": "open.spotify.com", "final_url": "", "explicit_name": True,
                "explicit_category": True, "spotify_song": dict(song),
                "spotify_format": self.settings.spotify_format,
                "spotify_bitrate": self.settings.spotify_bitrate,
                "provider": "YouTube Music via spotDL", "percent": 0,
            }
            self.tasks[task_id] = task
            self.store.save_downloads([task_id], self.tasks)
            self.store.event("spotDL track queued", name)
            log.info("[%s] QUEUED spotify track=%r artist=%r album=%r duration=%ss isrc=%s "
                     "format=%s bitrate=%s estimate=%s paused=%s path=%s",
                     task_id, song.get("name"), song.get("artist"), song.get("album_name"),
                     song.get("duration"), song.get("isrc") or "-", task["spotify_format"],
                     task["spotify_bitrate"], estimate or "n/a", bool(start_paused), path)
            log.debug("[%s] spotify metadata: %s", task_id, redact(sorted(song)))
            return dict(task)

    def action(self, task_id: str, action: str) -> dict:
        log.info("[%s] ACTION %s (status=%s)", task_id, action,
                 self.tasks.get(task_id, {}).get("status", "missing"))
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                raise KeyError("Download not found.")
            if action == "pause":
                if task["status"] in {"queued", "downloading"}:
                    if task_id in self.controls:
                        self.controls[task_id].set()
                        self.reasons[task_id] = "pause"
                    task["status"] = "paused"
                    task["speed"] = 0
                    self.store.event("Download paused", task["name"])
            elif action in {"resume", "retry"}:
                if task["status"] in {"paused", "error"}:
                    task["status"] = "queued"
                    task["error"] = ""
                    if action == "retry":
                        task["connections"] = 1
                    self.store.event("Download queued", task["name"])
                elif task["status"] == "completed" and action == "retry":
                    return self.add(url=task["url"], filename=task["name"], category=task["category"],
                                    source=task["source"])
            elif action == "delete":
                self._deleted_paths[task_id] = [task["part_path"], task["path"] + ".ndm.json"]
                if task["source"] in {"ytdlp", "spotdl"} and task["status"] != "completed":
                    final = Path(task["path"])
                    self._deleted_paths[task_id].append(str(final))
                    self._deleted_paths[task_id].extend(str(p) for p in final.parent.glob(final.stem + ".*")
                                                        if p.suffix in {".part", ".ytdl"})
                if task_id in self.controls:
                    self.controls[task_id].set()
                    self.reasons[task_id] = "delete"
                self.tasks.pop(task_id)
                self.store.save_downloads([task_id], self.tasks)
                self.store.event("Removed from downloads", task["name"])
                if task["status"] == "completed":
                    return {"deleted": task_id, "file_kept": True}
                if task_id not in self.threads:
                    Path(task["part_path"]).unlink(missing_ok=True)
                    Path(task["path"] + ".ndm.json").unlink(missing_ok=True)
                return {"deleted": task_id, "file_kept": False}
            else:
                raise ValueError("Unknown download action.")
            self.store.save_downloads([task_id], self.tasks)
            return dict(task)

    def update_settings(self, patch: SettingsPatch) -> Settings:
        data = self.settings.model_dump()
        changes = patch.model_dump(exclude_none=True)
        log.info("SETTINGS changed keys=%s", sorted(changes))
        candidate = Settings.model_validate({**data, **changes})
        with self.lock:
            was_running = self.settings.engine_running
            self.settings = candidate
            configure_environment(candidate)
            self.root = Path(candidate.download_dir).expanduser().resolve()
            self.root.mkdir(parents=True, exist_ok=True)
            self.store.set_meta("settings", candidate.model_dump())
            if not candidate.engine_running:
                for task_id, task in list(self.tasks.items()):
                    if task["status"] in {"downloading", "queued"}:
                        self.action(task_id, "pause")
                        task["engine_paused"] = True
                        self._mark(task_id)
            elif not was_running:
                for task_id, task in list(self.tasks.items()):
                    if task.pop("engine_paused", False) and task["status"] == "paused":
                        self.action(task_id, "resume")
            self.store.event("Preferences updated", "Your download engine is ready.")
        return candidate

    def capture(self, *, url: str, filename: str | None, mime: str, source: str) -> dict:
        if not self.settings.auto_capture:
            raise RuntimeError("Media capture is off. Enable it in Media grabber first.")
        url = self.validate_target(url)
        host = (urlsplit(url).hostname or "").lower()
        allowed = self.settings.allowed_domains
        if allowed and not any(host == d or host.endswith("." + d) for d in allowed):
            raise ValueError("This host is not in your capture allowlist.")
        kind = category_for(filename or url, mime)
        if kind not in self.settings.capture_types:
            raise ValueError("This media type is not enabled for capture.")
        if Path(urlsplit(url).path).suffix.lower() not in MEDIA_EXTENSIONS and not mime.startswith(("audio/", "video/")):
            raise ValueError("The link does not point to an audio or video file.")
        if Path(urlsplit(url).path).suffix.lower() in {".m3u8", ".mpd"}:
            return self.add_media(url=url, format_id="best", name=Path(urlsplit(url).path).stem or "media",
                                  category=kind, ext="mp4" if kind == "video" else "m4a")
        return self.add(url=url, filename=filename, category=kind, source=source)

    def _watch_inbox(self) -> None:
        if not (self.settings.auto_capture and self.settings.watch_inbox and self.settings.engine_running):
            return
        for path in (self.store.directory / "inbox").glob("*"):
            if path.suffix.lower() not in {".txt", ".url"} or not path.is_file():
                continue
            try:
                if path.stat().st_size > 1_000_000:
                    raise ValueError("Link files must be smaller than 1 MB.")
                text = path.read_text(encoding="utf-8-sig")
                urls = re.findall(r"(?:^URL=|^\s*)(https?://\S+)", text, re.MULTILINE | re.IGNORECASE)
                for url in urls[:100]:
                    try:
                        self.capture(url=url, filename=None, mime="", source="inbox")
                    except (ValueError, RuntimeError) as exc:
                        self.store.event("Link skipped", str(exc), "warning")
                path.rename(path.with_name(path.name + ".processed"))
            except (OSError, ValueError) as exc:
                self.store.event("Inbox could not process a file", str(exc), "warning")
                try:
                    path.rename(path.with_name(path.name + ".skipped"))
                except OSError:
                    pass

    def _loop(self) -> None:
        last_sample = 0.0
        last_inbox = 0.0
        while not self.stop.is_set():
            with self.lock:
                if self.settings.engine_running:
                    active = sum(t["status"] == "downloading" for t in self.tasks.values())
                    available = self.settings.max_concurrent - len(self.threads)
                    for task in sorted(self.tasks.values(), key=lambda t: t["created_at"]):
                        if available <= 0 or active >= self.settings.max_concurrent:
                            break
                        if task["status"] == "queued" and task["id"] not in self.threads:
                            self._start(task)
                            available -= 1
                            active += 1
                now = time.monotonic()
                if now - last_sample >= 1:
                    for task in self.tasks.values():
                        previous = task.get("_sample_bytes", task["downloaded"])
                        elapsed = now - task.get("_sample_time", now - 1)
                        raw = max(0, task["downloaded"] - previous) / max(elapsed, 0.01)
                        task["speed"] = round(task["speed"] * 0.35 + raw * 0.65) if task["status"] == "downloading" else 0
                        task["_sample_bytes"] = task["downloaded"]
                        task["_sample_time"] = now
                    self.speed_history.append(sum(t["speed"] for t in self.tasks.values()))
                    last_sample = now
                if now - last_inbox >= 3:
                    self._watch_inbox()
                    last_inbox = now
                if self.dirty:
                    ids = list(self.dirty)
                    self.dirty.clear()
                    self.store.save_downloads(ids, self.tasks)
            self.stop.wait(0.2)
        with self.lock:
            self.store.save_downloads(list(self.tasks), self.tasks)

    def _start(self, task: dict) -> None:
        task_id = task["id"]
        event = threading.Event()
        self.controls[task_id] = event
        self.reasons[task_id] = "pause"
        task["status"] = "downloading"
        task["error"] = ""
        task["_sample_bytes"] = task["downloaded"]
        task["_sample_time"] = time.monotonic()
        log.info("[%s] START source=%s name=%r connections=%s speed_limit=%s",
                 task_id, task.get("source"), task["name"], task.get("connections"),
                 self.settings.speed_limit or "unlimited")
        thread = threading.Thread(target=self._run, args=(task_id, event), name=f"ndm-{task_id}", daemon=True)
        self.threads[task_id] = thread
        thread.start()
        self._mark(task_id)

    def _progress(self, task_id: str) -> None:
        with self.lock:
            task = self.tasks.get(task_id)
            if task:
                task["downloaded"] = sum(s["done"] for s in task["segments"])
                self._mark(task_id)

    def _run(self, task_id: str, stop: threading.Event) -> None:
        if self.tasks.get(task_id, {}).get("source") == "spotdl":
            self._run_spotdl(task_id, stop)
            return
        if self.tasks.get(task_id, {}).get("source") == "ytdlp":
            self._run_ytdlp(task_id, stop)
            return
        track = Tracker(log, task_id)
        initial = self.tasks.get(task_id, {})
        track.stage("connecting", url=redact(initial.get("url", "")), name=initial.get("name"),
                    connections=initial.get("connections"))
        track.detail("config proxy=%s cookies=%s speed_limit=%s",
                     bool(self.settings.proxy_enabled), bool(self.settings.firefox_cookies),
                     self.settings.speed_limit or "unlimited")
        try:
            with httpx.Client(timeout=httpx.Timeout(20, read=30), follow_redirects=True,
                              headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
                              cookies=firefox_cookie_jar(self.settings), **httpx_options(self.settings),
                              event_hooks={"request": [lambda request: self.validate_target(str(request.url))]}) as client:
                for attempt in range(2):
                    try:
                        self._transfer(task_id, stop, client)
                        break
                    except RangeLost:
                        log.warning("[%s] server dropped range support; restarting with 1 connection", task_id)
                        track.stage("restart-single-connection")
                        if attempt == 1:
                            raise
                        with self.lock:
                            task = self.tasks.get(task_id)
                            if not task:
                                return
                            task.update(connections=1, segments=[], downloaded=0, resumable=False,
                                        etag="", last_modified="")
            with self.lock:
                task = self.tasks.get(task_id)
                if not task:
                    return
                if stop.is_set():
                    task["status"] = "paused"
                    task["speed"] = 0
                else:
                    size = Path(task["part_path"]).stat().st_size
                    if task["size"] and size != task["size"]:
                        raise OSError(f"Incomplete file: expected {task['size']} bytes, received {size}.")
                    destination = Path(task["path"])
                    if destination.exists():
                        destination = self._unique(destination.parent, destination.name)
                        task["path"] = str(destination)
                        task["name"] = destination.name
                    os.replace(task["part_path"], destination)
                    Path(task["path"] + ".ndm.json").unlink(missing_ok=True)
                    task.update(status="completed", size=size, size_estimated=False,
                                downloaded=size, speed=0, completed_at=time.time(), error="")
                    self.store.event("Download complete", task["name"], "success")
                    track.stage("completed", file=task["path"], bytes=size)
                    track.done(bytes=size, file=task["name"])
                self._mark(task_id)
        except Exception as exc:
            with self.lock:
                task = self.tasks.get(task_id)
                if task:
                    task.update(status="error", speed=0,
                                error=safe_error(exc, self.settings))
                    self._mark(task_id)
                    self.store.event("Download needs attention", task["name"] + ": " + task["error"], "warning")
                    track.failed(exc, settings=self.settings.model_dump(),
                                 url=redact(task.get("url", "")))
        finally:
            with self.lock:
                self.threads.pop(task_id, None)
                self.controls.pop(task_id, None)
                if self.reasons.pop(task_id, "pause") == "delete":
                    for path in self._deleted_paths.pop(task_id, []):
                        try:
                            Path(path).unlink(missing_ok=True)
                        except OSError:
                            pass

    def _run_spotdl(self, task_id: str, stop: threading.Event) -> None:
        from .spotify import SpotifyCancelled, download

        track = Tracker(log, task_id)
        try:
            with self.lock:
                task = self.tasks.get(task_id)
                if not task:
                    return
                snapshot = dict(task)
                settings = self.settings.model_dump()
            song = snapshot["spotify_song"]
            track.stage("queued", track=f"{song.get('artist')} - {song.get('name')}",
                        format=snapshot["spotify_format"], bitrate=snapshot["spotify_bitrate"])
            track.detail("config proxy=%s remote_dns=%s cookies=%s matcher=%s tagger=%s "
                         "rate_limit=%s connections=%s ffmpeg=%s",
                         bool(settings.get("proxy_enabled")), settings.get("proxy_remote_dns"),
                         bool(settings.get("firefox_cookies")), settings.get("spotify_matcher"),
                         settings.get("spotify_tagger"), self.settings.speed_limit or "unlimited",
                         snapshot["connections"], bool(shutil.which("ffmpeg")))
            track.stage("matching")

            def progress(downloaded, total, phase, percent=None, *, estimated=False):
                with self.lock:
                    current = self.tasks.get(task_id)
                    if not current:
                        stop.set()
                        return
                    if downloaded is not None:
                        current["downloaded"] = int(downloaded)
                    if total is not None:
                        current["transfer_size"] = int(total)
                        current["transfer_size_estimated"] = bool(estimated)
                    if percent is None and downloaded is not None and total:
                        percent = min(99.0, int(downloaded) / int(total) * 100)
                    if percent is not None:
                        current["percent"] = float(percent)
                    if phase != current.get("phase"):
                        track.stage(phase or "downloading")
                    current["phase"] = phase
                    track.progress(current.get("downloaded"), current.get("transfer_size"), phase or "")
                    self._mark(task_id)

            def on_match(matched: dict) -> None:
                with self.lock:
                    current = self.tasks.get(task_id)
                    if current:
                        current["final_url"] = matched.get("url", "")
                        current["provider"] = matched.get("provider") or "YouTube Music via spotDL"
                        track.detail("matched source=%s provider=%s score=%s",
                                     redact(matched.get("url", "")), current["provider"],
                                     matched.get("match_score", "n/a"))
                        self._mark(task_id)

            active = max(1, sum(t["status"] == "downloading" for t in self.tasks.values()))
            path = download(snapshot["spotify_song"], Path(snapshot["path"]), self.store.directory,
                            stop, progress, audio_format=snapshot["spotify_format"],
                            bitrate=snapshot["spotify_bitrate"], settings=settings,
                            rate_limit=self.settings.speed_limit // active,
                            connections=snapshot["connections"], on_match=on_match)
            with self.lock:
                task = self.tasks.get(task_id)
                if not task:
                    path.unlink(missing_ok=True)
                    return
                size = path.stat().st_size
                task.update(path=str(path), name=path.name, status="completed", size=size,
                            size_estimated=False, downloaded=size, speed=0, completed_at=time.time(),
                            percent=100, phase="complete", error="")
                self._mark(task_id)
                self.store.event("spotDL match downloaded", task["name"], "success")
            track.stage("completed", file=str(path), bytes=size)
            track.done(bytes=size, file=path.name)
        except Exception as exc:
            import yt_dlp

            with self.lock:
                task = self.tasks.get(task_id)
                if not task:
                    return
                if stop.is_set() or isinstance(exc, (SpotifyCancelled, yt_dlp.utils.DownloadCancelled)):
                    log.info("[%s] PAUSED (%s) after %.2fs", task_id,
                             type(exc).__name__, time.monotonic() - track.started)
                    task.update(status="paused", speed=0, phase="paused")
                else:
                    if isinstance(exc, yt_dlp.cookies.CookieLoadError):
                        message = FIREFOX_COOKIE_ERROR
                    else:
                        message = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))
                    task.update(status="error", speed=0, phase="error",
                                error=safe_error(ValueError(message), self.settings))
                    self.store.event("spotDL download needs attention", task["error"], "warning")
                    track.failed(exc, settings=settings, task=task["name"])
                self._mark(task_id)
        finally:
            with self.lock:
                self.threads.pop(task_id, None)
                self.controls.pop(task_id, None)
                if self.reasons.pop(task_id, "pause") == "delete":
                    for name in self._deleted_paths.pop(task_id, []):
                        Path(name).unlink(missing_ok=True)

    def _run_ytdlp(self, task_id: str, stop: threading.Event) -> None:
        import yt_dlp

        from .media import media_download

        track = Tracker(log, task_id)
        try:
            with self.lock:
                task = self.tasks.get(task_id)
                if not task:
                    return
                snapshot = dict(task)
                settings = self.settings.model_dump()
            track.stage("extracting", url=redact(snapshot["url"]), name=snapshot["name"])
            track.detail("config proxy=%s cookies=%s quality=%s format=%s",
                         bool(settings.get("proxy_enabled")), bool(settings.get("firefox_cookies")),
                         snapshot.get("media_quality"), snapshot.get("media_format") or "auto")

            def progress(downloaded, total, phase, *, estimated=False):
                with self.lock:
                    current = self.tasks.get(task_id)
                    if not current:
                        stop.set()
                        return
                    if downloaded is not None:
                        current["downloaded"] = downloaded
                    if total is not None:
                        current["size"] = total
                        current["size_estimated"] = bool(estimated)
                    if phase != current.get("phase"):
                        track.stage(phase or "downloading")
                    current["phase"] = phase
                    track.progress(current.get("downloaded"), current.get("size"), phase or "")
                    self._mark(task_id)

            active = max(1, sum(t["status"] == "downloading" for t in self.tasks.values()))
            path = media_download(snapshot, stop, progress,
                                  self.settings.speed_limit // active, snapshot["connections"], settings=settings)
            with self.lock:
                task = self.tasks.get(task_id)
                if not task:
                    path.unlink(missing_ok=True)
                    return
                size = path.stat().st_size
                task.update(path=str(path), name=path.name, status="completed", size=size,
                            size_estimated=False, downloaded=size, speed=0,
                            completed_at=time.time(), phase="complete", error="")
                self._mark(task_id)
                self.store.event("Media download complete", task["name"], "success")
            track.stage("completed", file=str(path), bytes=size)
            track.done(bytes=size, file=path.name)
        except Exception as exc:
            with self.lock:
                task = self.tasks.get(task_id)
                if not task:
                    return
                if stop.is_set() or isinstance(exc, yt_dlp.utils.DownloadCancelled):
                    task.update(status="paused", speed=0, phase="paused")
                else:
                    message = FIREFOX_COOKIE_ERROR if isinstance(exc, yt_dlp.cookies.CookieLoadError) else str(exc)
                    task.update(status="error", speed=0, error=safe_error(ValueError(message), self.settings), phase="error")
                    self.store.event("Media download needs attention", task["error"], "warning")
                    track.failed(exc, settings=settings, url=redact(snapshot["url"]))
                self._mark(task_id)
        finally:
            with self.lock:
                self.threads.pop(task_id, None)
                self.controls.pop(task_id, None)
                if self.reasons.pop(task_id, "pause") == "delete":
                    for path in self._deleted_paths.pop(task_id, []):
                        Path(path).unlink(missing_ok=True)

    def _transfer(self, task_id: str, stop: threading.Event, client: httpx.Client) -> None:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            url, part = task["url"], Path(task["part_path"])
            old_etag, old_modified = task["etag"], task["last_modified"]
            old_size, connections = task["size"], task["connections"]
            explicit_name = bool(task.get("explicit_name"))
        part.parent.mkdir(parents=True, exist_ok=True)
        with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as response:
            if response.status_code == 416 and old_size and part.exists() and part.stat().st_size == old_size:
                return
            response.raise_for_status()
            total = 0
            resumable = response.status_code == 206
            match = re.search(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", ""))
            if resumable and match:
                total = int(match.group(3))
            elif response.status_code == 200:
                total = int(response.headers.get("content-length", "0"))
            etag = response.headers.get("etag", "")
            modified = response.headers.get("last-modified", "")
            mime = response.headers.get("content-type", "").split(";")[0].lower()
            final_url = str(response.url)
            disposition = response.headers.get("content-disposition", "")
            name_match = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)", disposition, re.I)
            suggested = (name_match.group(1) or name_match.group(2)) if name_match else Path(unquote(urlsplit(final_url).path)).name
        log.info("[%s] PROBE status=%s bytes=%s resumable=%s mime=%s etag=%s final=%s",
                 task_id, response.status_code, total or "unknown", resumable, mime or "-",
                 (etag or "-")[:24], redact(final_url))
        if mime == "text/html" and Path(part).suffix.lower() not in {".html", ".htm"}:
            raise ValueError("This URL returns a webpage, not a file. Paste it into Media grabber to find its media.")
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            changed = bool(old_size and total != old_size or old_etag and etag != old_etag or old_modified and modified != old_modified)
            if changed:
                task["segments"] = []
            if not explicit_name and task["name"] == "download" and suggested:
                name = safe_filename(suggested)
                if name != "download":
                    path = self._unique(part.parent, name)
                    task.update(name=name, path=str(path), part_path=str(path) + ".part",
                                category=task["category"] if task.get("explicit_category") else category_for(name, mime))
                    part = Path(task["part_path"])
            task.update(size=total, size_estimated=False, resumable=resumable, etag=etag,
                        last_modified=modified, mime=mime, final_url=final_url)
            if not task["segments"]:
                count = min(connections, max(1, math.ceil(total / (1024 * 1024)))) if resumable and total else 1
                step = math.ceil(total / count) if total else 0
                task["segments"] = [
                    {"start": i * step, "end": min(total - 1, (i + 1) * step - 1) if total else 0, "done": 0}
                    for i in range(count)
                ]
                with part.open("wb") as handle:
                    if total and resumable:
                        handle.truncate(total)
            segments = [dict(s) for s in task["segments"]]
            log.info("[%s] TRANSFER segments=%d resumed_bytes=%d part=%s",
                     task_id, len(segments), sum(x["done"] for x in segments), task["part_path"])
            task["downloaded"] = sum(s["done"] for s in segments)
            self._mark(task_id)
        if stop.is_set():
            return
        # Persist validators before writing, so a crash cannot silently mix file versions.
        with self.lock:
            self.store.save_downloads([task_id], self.tasks)
        errors: list[Exception] = []
        with ThreadPoolExecutor(max_workers=len(segments), thread_name_prefix=f"ndm-seg-{task_id}") as pool:
            futures = [pool.submit(self._segment, task_id, i, stop, client, final_url) for i in range(len(segments))]
            for future in futures:
                try:
                    future.result()
                except Exception as exc:
                    errors.append(exc)
                    if not isinstance(exc, RangeLost):
                        # A paused/deleted task should not leave peers downloading indefinitely.
                        with self.lock:
                            if task_id not in self.tasks or self.tasks[task_id]["status"] == "error":
                                stop.set()
        if errors:
            raise errors[0]
        self._progress(task_id)

    def _segment(self, task_id: str, index: int, stop: threading.Event,
                 client: httpx.Client, url: str) -> None:
        for attempt in range(3):
            if stop.is_set():
                return
            with self.lock:
                task = self.tasks.get(task_id)
                if not task:
                    return
                segment = task["segments"][index]
                start, end, done = segment["start"], segment["end"], segment["done"]
                total, resumable, part = task["size"], task["resumable"], task["part_path"]
                if resumable and done >= end - start + 1:
                    return
                headers = {"Range": f"bytes={start + done}-{end}" if end else f"bytes={start + done}-"} if resumable else {}
                log.debug("[%s] segment %d attempt %d range=%s done=%d", task_id, index,
                          attempt + 1, headers.get("Range", "full"), done)
                if done and task["etag"] and not task["etag"].startswith('W/'):
                    headers["If-Range"] = task["etag"]
                elif done and task["last_modified"]:
                    headers["If-Range"] = task["last_modified"]
            try:
                with client.stream("GET", url, headers=headers) as response:
                    if response.status_code == 416 and resumable:
                        with self.lock:
                            if task_id in self.tasks:
                                self.tasks[task_id]["segments"][index]["done"] = end - start + 1
                        self._progress(task_id)
                        return
                    response.raise_for_status()
                    if resumable:
                        if response.status_code != 206:
                            raise RangeLost("The server stopped supporting ranges. Retrying with one connection.")
                        match = re.search(r"bytes (\d+)-", response.headers.get("content-range", ""))
                        if not match or int(match.group(1)) != start + done:
                            raise OSError("The server returned an unexpected byte range; partial data was not written.")
                    mode = "r+b" if resumable else "wb"
                    with open(part, mode) as handle:
                        handle.seek(start + done if resumable else 0)
                        written = 0
                        for chunk in response.iter_bytes(CHUNK):
                            if stop.is_set():
                                return
                            if resumable and done + written + len(chunk) > end - start + 1:
                                raise OSError("The server sent more bytes than the requested range.")
                            tick = time.monotonic()
                            handle.write(chunk)
                            written += len(chunk)
                            with self.lock:
                                if task_id not in self.tasks:
                                    return
                                self.tasks[task_id]["segments"][index]["done"] = done + written
                                limit = self.settings.speed_limit
                                active = max(1, sum(t["status"] == "downloading" for t in self.tasks.values()))
                                workers = len(self.tasks[task_id]["segments"])
                            self._progress(task_id)
                            if limit:
                                delay = len(chunk) / max(1, limit / active / workers)
                                stop.wait(max(0, delay - (time.monotonic() - tick)))
                            if not resumable and total and done + written > total:
                                raise OSError("The response exceeded its advertised size.")
                        handle.flush()
                with self.lock:
                    task = self.tasks.get(task_id)
                    if not task:
                        return
                    actual = task["segments"][index]["done"]
                    if resumable and actual < end - start + 1:
                        raise OSError("Connection closed before the requested range was complete.")
                    if not resumable:
                        size = Path(part).stat().st_size
                        task["size"] = size
                        task["segments"][index]["done"] = size
                        task["segments"][index]["end"] = size - 1
                self._progress(task_id)
                return
            except RangeLost:
                raise
            except (httpx.HTTPError, OSError) as exc:
                log.warning("[%s] segment %d attempt %d failed: %s: %s", task_id, index,
                            attempt + 1, type(exc).__name__, redact(exc))
                if attempt == 2 or stop.is_set():
                    raise
                stop.wait(0.7 * (2 ** attempt))

    def snapshot(self) -> dict:
        with self.lock:
            tasks = [{k: v for k, v in t.items() if not k.startswith("_")} for t in self.tasks.values()]
            tasks.sort(key=lambda t: t["created_at"], reverse=True)
            completed = [t for t in tasks if t["status"] == "completed"]
            today = time.strftime("%Y-%m-%d")
            completed_today = [t for t in completed if time.strftime("%Y-%m-%d", time.localtime(t["completed_at"] or 0)) == today]
            usage = shutil.disk_usage(self.root)
            return {
                "downloads": tasks,
                "settings": self.settings.model_dump(),
                "events": self.store.events(),
                "stats": {
                    "active": sum(t["status"] == "downloading" for t in tasks),
                    "queued": sum(t["status"] == "queued" for t in tasks),
                    "paused": sum(t["status"] == "paused" for t in tasks),
                    "completed": len(completed), "errors": sum(t["status"] == "error" for t in tasks),
                    "total": len(tasks), "speed": sum(t["speed"] for t in tasks),
                    "completed_today": len(completed_today),
                    "bytes_today": sum(t["size"] for t in completed_today),
                    "bytes_total": sum(t["size"] for t in completed),
                    "disk_free": usage.free, "disk_total": usage.total,
                    "connections": sum(len(t["segments"]) for t in tasks if t["status"] == "downloading"),
                },
                "history": list(self.speed_history),
                "daemon": {"uptime": time.time() - self.started, "pid": os.getpid(),
                           "version": "0.1.0", "data_dir": str(self.store.directory),
                           "ffmpeg": bool(shutil.which("ffmpeg")),
                           "inbox": str(self.store.directory / "inbox")},
            }

    def library(self) -> list[dict]:
        files = []
        roots = [self.root]
        music = Path(self.settings.music_dir).expanduser()
        roots.append(music)
        seen = set()
        for root in roots:
            if not root.is_dir():
                continue
            for directory, dirs, names in os.walk(root):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for name in names:
                    path = Path(directory) / name
                    if category_for(name) != "audio" or path.suffix == ".part" or str(path) in seen:
                        continue
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    seen.add(str(path))
                    files.append({"id": hashlib.sha256(str(path).encode()).hexdigest()[:16],
                                  "name": name, "size": stat.st_size, "modified": stat.st_mtime,
                                  "path": str(path), "folder": str(path.parent)})
                    if len(files) >= 500:
                        return sorted(files, key=lambda f: f["modified"], reverse=True)
        return sorted(files, key=lambda f: f["modified"], reverse=True)

    def shutdown(self) -> None:
        self.stop.set()
        with self.lock:
            for task_id, event in self.controls.items():
                self.reasons[task_id] = "pause"
                event.set()
            for task in self.tasks.values():
                if task["status"] in {"downloading", "queued"}:
                    task["status"] = "paused"
                    task["speed"] = 0
        self.thread.join(timeout=5)
        for thread in list(self.threads.values()):
            thread.join(timeout=5)
        self.store.save_downloads(list(self.tasks), self.tasks)
        self.store.close()
