"""Shared, validated configuration and URL helpers."""
from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ACCENTS = ["#7856e8", "#4778e6", "#169c83", "#e29342", "#dc657e", "#64748b"]
# Every semantic color exposed by Nexa can be overridden independently.
CUSTOM_COLOR_KEYS = {
    "background", "surface", "surface_raised", "surface_muted", "foreground",
    "text_secondary", "text_muted", "border", "border_subtle", "border_strong",
    "accent", "accent_strong", "accent_subtle", "accent_foreground",
    "success", "warning", "danger", "info", "control_on", "control_glyph",
    "control_track", "control_track_hover", "control_thumb", "control_thumb_on",
    "hover", "active", "scrim", "focus_ring", "series_1", "series_2",
    "series_3", "series_4", "series_5", "series_6",
}
PROXY_SCHEMES = {"http": 80, "https": 443, "socks5": 1080, "socks5h": 1080}
# Accept the common "sockcs5" typo and save it as the standard SOCKS5 scheme.
class SpotifyCancelled(Exception):
    """Cooperative stop for a Spotify job. Defined here so the tagger and the
    Spotify adapter can both raise it without importing each other."""


PROXY_SCHEME_ALIASES = {"sockcs5": "socks5"}
# Bump when the daemon's settings schema or local IPC payload contract changes
# so the desktop can replace an out-of-date background daemon instead of sending
# it fields it cannot accept.
SETTINGS_SCHEMA_VERSION = 9
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


def validate_url(url: str, *, allow_private: bool = False, resolve: bool = True) -> str:
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
            literal = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            literal = None
        if literal is not None:
            if not literal.is_global:
                raise ValueError("Private and local network URLs are blocked by this daemon's settings.")
        elif resolve:
            try:
                addresses = socket.getaddrinfo(parsed.hostname, port or (443 if parsed.scheme == "https" else 80))
            except socket.gaierror as exc:
                raise ValueError("The host could not be resolved. Check the URL and your connection.") from exc
            for item in addresses:
                address = ipaddress.ip_address(item[4][0])
                if not address.is_global:
                    raise ValueError("Private and local network URLs are blocked by this daemon's settings.")
    return url


def normalize_color(value: str) -> str:
    value = value.strip().lower()
    if not re.fullmatch(r"#[0-9a-f]{6}(?:[0-9a-f]{2})?", value):
        raise ValueError("Use a hex color such as #1f2430 or #1f2430ff.")
    return value


def normalize_proxy_url(value: str) -> str:
    """Validate HTTP/HTTPS/SOCKS5 proxy URLs and fill in conventional ports."""
    from urllib.parse import quote

    value = value.strip()
    if not value:
        return ""
    if len(value) > 2048:
        raise ValueError("Proxy URL is too long.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Enter a valid proxy URL, for example socks5://127.0.0.1:1080.") from exc
    scheme = (parsed.scheme or "").lower()
    scheme = PROXY_SCHEME_ALIASES.get(scheme, scheme)
    if scheme not in PROXY_SCHEMES:
        raise ValueError("Proxy scheme must be http, https, socks5, or socks5h.")
    host = parsed.hostname
    if not host or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Proxy URL must be only scheme://[user:password@]host[:port].")
    if port is None:
        port = PROXY_SCHEMES[scheme]
    if not 1 <= port <= 65535:
        raise ValueError("Proxy port must be between 1 and 65535.")
    userinfo = ""
    if parsed.username is not None:
        userinfo = quote(unquote(parsed.username), safe="")
        if parsed.password is not None:
            userinfo += ":" + quote(unquote(parsed.password), safe="")
        userinfo += "@"
    host_part = f"[{host}]" if ":" in host else host
    return f"{scheme}://{userinfo}{host_part}:{port}"


def redact_proxy_url(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}"
    except ValueError:
        return "proxy"


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
    colors: dict[str, str] = Field(default_factory=dict)
    proxy_enabled: bool = False
    proxy_url: str = ""
    proxy_username: str = Field(default="", max_length=256)
    proxy_password: str = Field(default="", max_length=256)
    proxy_remote_dns: bool = True
    auto_capture: bool = False
    clipboard_capture: bool = False
    watch_inbox: bool = False
    capture_types: list[str] = Field(default_factory=lambda: ["video", "audio"])
    allowed_domains: list[str] = Field(default_factory=list)
    media_quality: str = "best"
    firefox_cookies: bool = False
    spotify_format: str = "mp3"
    spotify_bitrate: str = "192k"
    # "ndm" writes tags directly (None-safe); "spotdl" keeps upstream's embedder
    # as an escape hatch while the new writer is validated on real libraries.
    spotify_tagger: str = "ndm"
    # Matching stays on spotDL until the NDM scorer is compared on real
    # playlists; `ndm match-check URL` reports where the two disagree.
    spotify_matcher: str = "spotdl"
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
        return normalize_color(value)

    @field_validator("colors")
    @classmethod
    def valid_colors(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = set(value) - CUSTOM_COLOR_KEYS
        if unknown:
            raise ValueError("Unknown color setting: " + ", ".join(sorted(unknown)))
        return {key: normalize_color(color) for key, color in value.items()}

    @field_validator("proxy_url")
    @classmethod
    def valid_proxy(cls, value: str) -> str:
        return normalize_proxy_url(value)

    @field_validator("proxy_username", "proxy_password")
    @classmethod
    def no_proxy_crlf(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("Proxy credentials cannot contain line breaks.")
        return value

    @model_validator(mode="after")
    def proxy_complete(self) -> Settings:
        if self.proxy_enabled and not self.proxy_url:
            raise ValueError("A proxy URL is required when proxy support is enabled.")
        if self.proxy_password and not self.proxy_username:
            raise ValueError("A proxy username is required with a proxy password.")
        return self

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

    @field_validator("spotify_matcher")
    @classmethod
    def valid_spotify_matcher(cls, value: str) -> str:
        if value not in {"ndm", "spotdl"}:
            raise ValueError("Choose the NDM or spotDL matcher.")
        return value

    @field_validator("spotify_tagger")
    @classmethod
    def valid_spotify_tagger(cls, value: str) -> str:
        if value not in {"ndm", "spotdl"}:
            raise ValueError("Choose the NDM or spotDL tag writer.")
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
    colors: dict[str, str] | None = None
    proxy_enabled: bool | None = None
    proxy_url: str | None = None
    proxy_username: str | None = None
    proxy_password: str | None = None
    proxy_remote_dns: bool | None = None
    auto_capture: bool | None = None
    clipboard_capture: bool | None = None
    watch_inbox: bool | None = None
    capture_types: list[str] | None = None
    allowed_domains: list[str] | None = None
    media_quality: str | None = None
    firefox_cookies: bool | None = None
    spotify_format: str | None = None
    spotify_bitrate: str | None = None
    spotify_tagger: str | None = None
    spotify_matcher: str | None = None
    notifications: bool | None = None


class NewDownload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=8192)
    filename: str | None = Field(default=None, max_length=180)
    category: str | None = None
    connections: int | None = Field(default=None, ge=1, le=8)
    start_paused: bool = False
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


class CaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=8192)
    filename: str | None = Field(default=None, max_length=180)
    mime: str = Field(default="", max_length=200)
    source: str = Field(default="clipboard", pattern="^(clipboard|inbox)$")
