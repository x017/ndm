"""Shared, validated configuration and URL helpers."""
from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

ACCENTS = ["#7856e8", "#4778e6", "#169c83", "#e29342", "#dc657e", "#64748b"]
MEDIA_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".mp3", ".m4a", ".wav",
    ".flac", ".ogg", ".opus", ".aac", ".m3u8", ".mpd",
}
CATEGORIES = {
    "video": {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"},
    "audio": {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac"},
    "image": {".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".avif", ".bmp"},
    "archive": {".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tgz"},
    "document": {".pdf", ".epub", ".doc", ".docx", ".txt", ".csv", ".xls", ".xlsx"},
}


def default_data_dir() -> Path:
    import os
    if env := os.environ.get("NDM_HOME"):
        return Path(env).expanduser().resolve()
    return Path.home() / ".local" / "share" / "ndm"


def default_download_dir() -> Path:
    return Path.home() / "Downloads" / "NDM"


def category_for(name: str, mime: str = "") -> str:
    suffix = Path(urlsplit(name).path).suffix.lower()
    for category, extensions in CATEGORIES.items():
        if suffix in extensions:
            return category
    if mime.startswith(("video/", "application/vnd.apple.mpegurl", "application/dash+xml")):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith(("application/pdf", "text/")):
        return "document"
    return "other"


def safe_filename(name: str, fallback: str = "download") -> str:
    name = unquote(name).split("?")[0].replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', "", name).strip(" .")
    name = name[:180] or fallback
    if name.lower() in {"con", "prn", "aux", "nul"}:
        name = f"_{name}"
    return name


def validate_url(url: str, *, allow_private: bool = False) -> str:
    """Reject credentials, non-HTTP schemes and optionally private targets."""
    url = url.strip()
    if len(url) > 8192:
        raise ValueError("This URL is too long.")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Enter a valid HTTP or HTTPS URL.") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Use an HTTP or HTTPS URL without embedded credentials.")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid URL port.")
    if not allow_private:
        try:
            addresses = socket.getaddrinfo(parsed.hostname, port or (443 if parsed.scheme == "https" else 80))
        except socket.gaierror as exc:
            raise ValueError("The host could not be resolved. Check the URL and your connection.") from exc
        for item in addresses:
            address = ipaddress.ip_address(item[4][0])
            if not address.is_global:
                raise ValueError("Private and local network URLs are blocked by this daemon's settings.")
    return url


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)
    download_dir: str = Field(default_factory=lambda: str(default_download_dir()))
    music_dir: str = Field(default_factory=lambda: str(Path.home() / "Music"))
    max_concurrent: int = Field(default=3, ge=1, le=8)
    connections: int = Field(default=4, ge=1, le=8)
    speed_limit: int = Field(default=0, ge=0, le=100_000_000)  # bytes/sec, total; 0 = unlimited
    engine_running: bool = True
    resume_on_start: bool = True
    theme: str = "light"
    accent: str = ACCENTS[0]
    auto_capture: bool = False
    clipboard_capture: bool = False
    watch_inbox: bool = False
    capture_types: list[str] = Field(default_factory=lambda: ["video", "audio"])
    allowed_domains: list[str] = Field(default_factory=list)
    media_quality: str = "best"
    spotify_format: str = "mp3"
    spotify_bitrate: str = "192k"
    notifications: bool = True

    @field_validator("theme")
    @classmethod
    def valid_theme(cls, value: str) -> str:
        if value not in {"light", "dark", "system"}:
            raise ValueError("Choose light, dark, or system.")
        return value

    @field_validator("accent")
    @classmethod
    def valid_accent(cls, value: str) -> str:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValueError("Use a six-digit hex color, such as #7856e8.")
        return value.lower()

    @field_validator("media_quality")
    @classmethod
    def valid_quality(cls, value: str) -> str:
        if value not in {"best", "1080", "720", "audio"}:
            raise ValueError("Unsupported media quality.")
        return value

    @field_validator("spotify_format")
    @classmethod
    def valid_spotify_format(cls, value: str) -> str:
        if value not in {"mp3", "m4a", "flac", "opus", "ogg"}:
            raise ValueError("Choose MP3, M4A, FLAC, Opus, or Ogg.")
        return value

    @field_validator("spotify_bitrate")
    @classmethod
    def valid_spotify_bitrate(cls, value: str) -> str:
        if value not in {"auto", "128k", "192k", "256k", "320k", "disable"}:
            raise ValueError("Unsupported audio conversion bitrate.")
        return value

    @field_validator("capture_types")
    @classmethod
    def valid_types(cls, value: list[str]) -> list[str]:
        if any(v not in {"video", "audio"} for v in value):
            raise ValueError("Capture supports video and audio.")
        return list(dict.fromkeys(value))

    @field_validator("allowed_domains")
    @classmethod
    def valid_domains(cls, value: list[str]) -> list[str]:
        result = []
        for domain in value[:100]:
            domain = domain.lower().strip()
            if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,63}", domain):
                raise ValueError("Enter hostnames only, without a scheme or path.")
            result.append(domain)
        return list(dict.fromkeys(result))


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    download_dir: str | None = None
    music_dir: str | None = None
    max_concurrent: int | None = Field(default=None, ge=1, le=8)
    connections: int | None = Field(default=None, ge=1, le=8)
    speed_limit: int | None = Field(default=None, ge=0, le=100_000_000)
    engine_running: bool | None = None
    resume_on_start: bool | None = None
    theme: str | None = None
    accent: str | None = None
    auto_capture: bool | None = None
    clipboard_capture: bool | None = None
    watch_inbox: bool | None = None
    capture_types: list[str] | None = None
    allowed_domains: list[str] | None = None
    media_quality: str | None = None
    spotify_format: str | None = None
    spotify_bitrate: str | None = None
    notifications: bool | None = None


class NewDownload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=8192)
    filename: str | None = Field(default=None, max_length=180)
    category: str | None = None
    connections: int | None = Field(default=None, ge=1, le=8)
    start_paused: bool = False
    authorized: bool = False
    source: str = Field(default="manual", max_length=32)

    @field_validator("category")
    @classmethod
    def valid_category(cls, value: str | None) -> str | None:
        if value is not None and value not in {*CATEGORIES, "other"}:
            raise ValueError("Unknown category.")
        return value


class MediaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=8192)
    authorized: bool = False


class CaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=8192)
    filename: str | None = Field(default=None, max_length=180)
    mime: str = Field(default="", max_length=200)
    source: str = Field(default="clipboard", pattern="^(clipboard|inbox)$")
    authorized: bool = False
