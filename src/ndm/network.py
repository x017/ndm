"""Shared proxy and opt-in browser-cookie configuration for network clients."""
from __future__ import annotations

import os
import threading
from http.cookiejar import CookieJar
from urllib.parse import quote, urlsplit

from .models import Settings, redact_proxy_url

FIREFOX_COOKIE_ERROR = (
    "Could not read Firefox cookies. Check that Firefox and a readable profile "
    "are installed, or turn off Firefox cookies in Options → Capture."
)
_ENV_LOCK = threading.RLock()
_ORIGINAL_ENV: dict[str, str | None] | None = None
_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy")


def proxy_url(settings: Settings | dict) -> str:
    """Return the effective explicit proxy, with safely encoded credentials."""
    data = settings.model_dump() if isinstance(settings, Settings) else settings
    if not data.get("proxy_enabled"):
        return ""
    value = data.get("proxy_url", "")
    if not value:
        raise ValueError("A proxy URL is required when proxy support is enabled.")
    parsed = urlsplit(value)
    username = data.get("proxy_username") or parsed.username or ""
    password = data.get("proxy_password") or parsed.password or ""
    scheme = parsed.scheme
    # Treat only an explicit False as "off": a None from a partial payload must
    # not silently downgrade to local DNS, which breaks censored-network lookups.
    if scheme == "socks5" and data.get("proxy_remote_dns") is not False:
        scheme = "socks5h"
    auth = ""
    if username:
        auth = quote(username, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        auth += "@"
    return f"{scheme}://{auth}{parsed.netloc.rsplit('@', 1)[-1]}"


def httpx_options(settings: Settings | dict) -> dict:
    url = proxy_url(settings)
    # An explicit NDM proxy takes precedence over ambient environment proxies.
    return {"proxy": url, "trust_env": False} if url else {"trust_env": True}


def ytdlp_options(settings: Settings | dict) -> dict:
    url = proxy_url(settings)
    options = {"proxy": url} if url else {}
    options.update(firefox_cookie_options(settings))
    return options


def firefox_cookie_options(settings: Settings | dict) -> dict:
    """Read Firefox lazily for each yt-dlp operation, only after opting in."""
    data = settings.model_dump() if isinstance(settings, Settings) else settings
    return {"cookiesfrombrowser": ("firefox",)} if data.get("firefox_cookies") else {}


def firefox_cookie_jar(settings: Settings | dict) -> CookieJar | None:
    """A domain-matching cookie jar for HTML inspection and segmented HTTP downloads.

    Read a fresh snapshot for each operation so logging in/out in Firefox takes
    effect without restarting NDM. Values stay in memory, never in NDM's store.
    Do not expose upstream exception text: it may contain private profile data.
    """
    data = settings.model_dump() if isinstance(settings, Settings) else settings
    if not data.get("firefox_cookies"):
        return None
    from yt_dlp.cookies import extract_cookies_from_browser
    try:
        return extract_cookies_from_browser("firefox")
    except Exception as exc:
        raise ValueError(FIREFOX_COOKIE_ERROR) from exc


def requests_proxies(settings: Settings | dict) -> dict[str, str] | None:
    url = proxy_url(settings)
    return {"http": url, "https": url} if url else None


def configure_environment(settings: Settings | dict) -> None:
    """Apply proxy env vars in the dedicated daemon for upstream metadata clients.

    requests/spotipy and curl_cffi/spotapi create their own sessions. Environment
    configuration covers those legs without monkey-patching upstream internals.
    Explicit httpx/yt-dlp options above still take precedence for transfers.
    """
    global _ORIGINAL_ENV
    with _ENV_LOCK:
        if _ORIGINAL_ENV is None:
            _ORIGINAL_ENV = {key: os.environ.get(key) for key in _ENV_KEYS}
        url = proxy_url(settings)
        if url:
            for key in _ENV_KEYS:
                os.environ[key] = "" if key.lower() == "no_proxy" else url
        else:
            for key, value in _ORIGINAL_ENV.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def safe_error(exc: BaseException, settings: Settings | dict) -> str:
    """Keep proxy credentials out of queue errors, events, and the desktop."""
    text = str(exc) or exc.__class__.__name__
    data = settings.model_dump() if isinstance(settings, Settings) else settings
    secrets = [data.get("proxy_password", ""), data.get("proxy_username", "")]
    try:
        parsed = urlsplit(data.get("proxy_url", ""))
        secrets.extend([parsed.password or "", parsed.username or ""])
    except ValueError:
        pass
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, "***").replace(quote(secret, safe=""), "***")
    return text[:500]


def proxy_label(settings: Settings | dict) -> str:
    data = settings.model_dump() if isinstance(settings, Settings) else settings
    return redact_proxy_url(data.get("proxy_url", "")) if data.get("proxy_enabled") else "Direct"
