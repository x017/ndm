"""Readable failure diagnostics: a redacted log file, upstream logs, and network hints.

Download failures used to surface only a short masked message, because yt-dlp was
created with ``quiet``/``no_warnings`` and spotDL's logger was pinned to ERROR.
Attaching a logger object to yt-dlp bypasses both switches, so the real network
warnings reach this module instead of being discarded.

Everything written here passes a redaction filter: proxy credentials, cookie
values, and Authorization headers never reach the log file.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import re
import shutil
import sys
import traceback
from pathlib import Path

LOG_FILENAME = "ndm.log"
_MAX_BYTES = 2_000_000
_BACKUPS = 3
_CONFIGURED: Path | None = None
_SECRETS: set[str] = set()

# Patterns that must never be persisted, even when a library logs a full request.
_COOKIE_RE = re.compile(r"(?i)\b(set-cookie|cookie|authorization|proxy-authorization)\s*[:=]\s*[^\r\n]+")
_URL_AUTH_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def debug_enabled() -> bool:
    """Verbose upstream logging is opt-in via NDM_DEBUG=1 (or --debug)."""
    return os.environ.get("NDM_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}


def log_dir(data_dir: Path) -> Path:
    return Path(data_dir).expanduser().resolve() / "logs"


def log_path(data_dir: Path) -> Path:
    return log_dir(data_dir) / LOG_FILENAME


def register_secrets(settings: dict | None) -> None:
    """Remember values that must be masked if a library echoes them back."""
    data = settings or {}
    for key in ("proxy_password", "proxy_username"):
        value = data.get(key)
        if value:
            _SECRETS.add(str(value))
    url = str(data.get("proxy_url") or "")
    match = re.match(r"(?i)^[a-z][a-z0-9+.-]*://([^/\s:@]+)(?::([^/\s@]*))?@", url)
    if match:
        _SECRETS.update(v for v in match.groups() if v)


def redact(text: str) -> str:
    """Strip credentials/cookies from any string destined for the log or the UI."""
    value = _ANSI_RE.sub("", str(text))
    for secret in sorted((s for s in _SECRETS if s and len(s) > 2), key=len, reverse=True):
        value = value.replace(secret, "***")
    value = _URL_AUTH_RE.sub(r"\1***:***@", value)
    return _COOKIE_RE.sub(lambda m: f"{m.group(1)}: ***", value)


_UPSTREAM_PREFIXES = ("spotdl", "ndm.ytdlp", "ndm.spotify.match", "urllib3", "httpx", "httpcore")


class _ConsoleQuietFilter(logging.Filter):
    """Keep upstream chatter in the file, not on the user's terminal.

    ``run_daemon`` installs a console handler via ``basicConfig``; without this,
    every spotDL/yt-dlp INFO line would also print to stdout/the service log.
    With NDM_DEBUG=1 the user asked for verbose output, so nothing is filtered.
    """

    def __init__(self, verbose: bool) -> None:
        super().__init__()
        self.verbose = verbose

    def filter(self, record: logging.LogRecord) -> bool:
        if self.verbose or record.levelno >= logging.WARNING:
            return True
        return not record.name.startswith(_UPSTREAM_PREFIXES)


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()
        if record.exc_info:
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        return True


def setup_logging(data_dir: Path, *, debug: bool | None = None) -> Path:
    """Attach a rotating, redacted file handler to the root logger (idempotent)."""
    global _CONFIGURED
    path = log_path(data_dir)
    if _CONFIGURED == path:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    verbose = debug_enabled() if debug is None else debug
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8", delay=False)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"))
    handler.addFilter(_RedactingFilter())
    handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_ndm_handler", False):
            root.removeHandler(existing)
    handler._ndm_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    for existing in root.handlers:
        if existing is handler or getattr(existing, "_ndm_handler", False):
            continue
        if not any(isinstance(f, _ConsoleQuietFilter) for f in existing.filters):
            existing.addFilter(_ConsoleQuietFilter(verbose))
        if not any(isinstance(f, _RedactingFilter) for f in existing.filters):
            existing.addFilter(_RedactingFilter())
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # Upstream chatter stays in the file; it must not spam the user's terminal.
    logging.getLogger("spotdl").setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _CONFIGURED = path
    return path


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def configure_upstream_logging() -> None:
    """Keep spotDL's matching detail available in the log instead of discarding it."""
    logging.getLogger("spotdl").setLevel(logging.DEBUG if debug_enabled() else logging.INFO)


class YtdlpLogger:
    """Route yt-dlp output into the log file.

    Supplying this object makes yt-dlp ignore ``quiet`` and ``no_warnings``, which
    is exactly what we want: signature/JS-runtime/HTTP warnings are the detail
    needed to explain a failed transfer.
    """

    def __init__(self, name: str = "ndm.ytdlp", context: str = "") -> None:
        self.log = logging.getLogger(name)
        self.context = context
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def _emit(self, level: int, message: str) -> None:
        text = redact(message).strip()
        if text:
            self.log.log(level, "%s%s", f"{self.context} " if self.context else "", text)

    def debug(self, message: str) -> None:
        self._emit(logging.DEBUG, message)

    def info(self, message: str) -> None:
        self._emit(logging.INFO, message)

    def warning(self, message: str) -> None:
        text = redact(message).strip()
        if text:
            self.warnings.append(text)
        self._emit(logging.WARNING, message)

    def error(self, message: str) -> None:
        text = redact(message).strip()
        if text:
            self.errors.append(text)
        # yt-dlp routes deprecation notices through error(); they are not failures.
        self._emit(logging.DEBUG if "Deprecated Feature" in text else logging.ERROR, message)

    def summary(self) -> str:
        """Last few upstream messages, for the queue row when a transfer fails."""
        items = (self.errors + self.warnings)[-3:]
        return " | ".join(dict.fromkeys(items))[:400]


def exception_chain(exc: BaseException) -> list[BaseException]:
    """Follow cause/context so a wrapped failure still exposes its real origin."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(chain) < 10:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return chain


def log_failure(logger: logging.Logger, stage: str, exc: BaseException, **fields) -> None:
    """Write the full, redacted failure chain plus the stage that produced it."""
    context = " ".join(f"{k}={redact(v)}" for k, v in fields.items() if v not in (None, ""))
    logger.error("FAILED at %s%s: %s: %s", stage, f" ({context})" if context else "",
                 type(exc).__name__, redact(exc))
    for depth, item in enumerate(exception_chain(exc)):
        logger.error("  cause[%d] %s: %s", depth, type(item).__name__, redact(item))
    logger.debug("Traceback for %s:\n%s", stage,
                 redact("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))))
    hint = diagnose(exc)
    if hint:
        logger.error("  hint: %s", hint)


_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)cookie(s)? (file|database|profile)|could not (read|find|extract).*cookie|CookieLoadError"),
     "Firefox cookies could not be read. Turn off Firefox cookies in Options → Capture, or install/sign in to Firefox."),
    (re.compile(r"(?i)sign in to confirm|not a bot|confirm you're not a bot|account.*terminated"),
     "YouTube is asking for a sign-in/bot check. Enable Firefox cookies (Options → Capture) after signing in to YouTube in Firefox, then retry."),
    (re.compile(r"(?i)no supported javascript runtime|signature solving failed|n challenge|nsig|jsinterp|deno"),
     "This source needs a JavaScript runtime. Install Deno system-wide or run `spotdl --download-deno`, then retry."),
    # A wrapped SSLError often *contains* an EOF; test that specific shape first
    # so a blocked handshake is not misreported as a certificate problem.
    (re.compile(r"(?i)tls/ssl connection has been closed|unexpected_eof|eof occurred in violation of protocol"
                r"|wrong_version_number|handshake failure|sslv3_alert|zero_return|ssl_error"),
     "The TLS handshake was cut off before completing. A firewall, VPN, or proxy is blocking or "
     "intercepting the connection; try another network or fix the proxy settings."),
    (re.compile(r"(?i)certificate_verify_failed|sslcertificateverify|self.signed certificate|ssl: |ssLError"),
     "TLS certificate validation failed. Check system clock/CA certificates, or a TLS-intercepting proxy or antivirus."),
    (re.compile(r"(?i)cannot connect to proxy|proxyerror|socks|proxy connection|unable to connect to proxy|407"),
     "The proxy refused or failed the connection. Verify the proxy URL, port, and credentials in Options → Network."),
    (re.compile(r"(?i)getaddrinfo|name or service not known|temporary failure in name resolution|nodename nor servname|dns"),
     "DNS lookup failed. Check your connection/VPN; with a SOCKS proxy try enabling remote DNS."),
    (re.compile(r"(?i)429|too many requests|rate.?limit"),
     "The source is rate-limiting you. Wait a few minutes, lower concurrency in Options, then retry."),
    # Specific causes must precede the generic "not found"/"unavailable" rule
    # below, or "ffmpeg not found" would be misreported as a missing source.
    (re.compile(r"(?i)ffmpeg|ffprobe|postprocessing|audio conversion failed"),
     "FFmpeg failed or is missing. Confirm ffmpeg and ffprobe are on PATH and support the selected output format."),
    (re.compile(r"(?i)no results found|found no youtube music match|no match|could not match"),
     "spotDL found no YouTube Music match for this track. Retry, or check the track title/artist."),
    (re.compile(r"(?i)requested format is not available|only images are available|no video formats"),
     "No usable audio format was returned. This is usually a missing JavaScript runtime (Deno) or a region/sign-in restriction."),
    (re.compile(r"(?i)\b403\b|forbidden|blocked|not available in your country|\bgeo\b|region"),
     "The source refused the request (403/region block). A different network/VPN region or signed-in cookies may be required."),
    (re.compile(r"(?i)timed out|timeout|readtimeout|connecttimeout"),
     "The connection timed out. Check connectivity/proxy, then retry."),
    (re.compile(r"(?i)connection refused|network is unreachable|no route to host|connection reset|remote end closed|connection aborted"),
     "The network dropped or refused the connection. Check VPN/firewall/proxy, then retry."),
    (re.compile(r"(?i)\b404\b|not found|no longer available|unavailable|removed by the uploader"),
     "The matched source is gone or unavailable. Retry to select a different match."),
)


def diagnose(exc: BaseException | str) -> str:
    """Map a failure (including wrapped causes) to one actionable sentence."""
    text = exc if isinstance(exc, str) else " ".join(
        f"{type(item).__name__} {item}" for item in exception_chain(exc))
    text = redact(text)
    for pattern, hint in _HINTS:
        if pattern.search(text):
            return hint
    return ""


def failure_message(exc: BaseException, *, detail: str = "", prefix: str = "",
                    limit: int = 460) -> str:
    """Build a queue-row message that survives safe_error's 500-char truncation.

    The actionable hint goes first: yt-dlp's raw text is often longer than the
    whole budget, so putting the hint last would hide the only useful sentence.
    """
    def tidy(text: str) -> str:
        # yt-dlp repeats the cause inside its own message; keep the leading part.
        text = redact(text).split(" (caused by ")[0].split("; please report this issue")[0].strip()
        return text[7:].strip() if text.startswith("ERROR: ") else text

    hint = diagnose(detail or exc)
    raw = tidy(str(exc)) or type(exc).__name__
    parts = [p for p in (prefix, hint) if p]
    head = " | ".join(parts)
    budget = max(80, limit - len(head) - 24)
    body = raw if len(raw) <= budget else raw[:budget].rstrip() + "…"
    extra = tidy(detail)
    # Only append upstream text that actually adds something the row lacks.
    if extra and extra[:60] not in raw and raw[:60] not in extra:
        room = budget - len(body) - 14
        if room > 40:
            body = f"{body} | upstream: {extra[:room]}"
    if body:
        parts.append(body)
    parts.append("full log: ndm logs")
    return " | ".join(parts)


def environment(settings: dict | None = None) -> dict:
    """Cheap, offline facts that explain most 'works for me' failures."""
    data = settings or {}
    info = {
        "ndm_python": sys.version.split()[0],
        "platform": platform.platform(),
        "ffmpeg": shutil.which("ffmpeg") or "MISSING",
        "ffprobe": shutil.which("ffprobe") or "MISSING",
        "deno": shutil.which("deno") or "not on PATH",
        "proxy_enabled": bool(data.get("proxy_enabled")),
        "firefox_cookies": bool(data.get("firefox_cookies")),
        "debug": debug_enabled(),
    }
    for module in ("yt_dlp", "spotdl", "ytmusicapi", "httpx", "mutagen"):
        try:
            mod = __import__(module)
            info[module] = getattr(mod, "__version__", None) or getattr(
                getattr(mod, "version", None), "__version__", "installed")
        except Exception:
            info[module] = "MISSING"
    return info


def log_environment(logger: logging.Logger, settings: dict | None = None) -> None:
    register_secrets(settings)
    facts = environment(settings)
    logger.info("Environment: %s", " ".join(f"{k}={redact(v)}" for k, v in facts.items()))


def check_connectivity(settings: dict | None = None, *, timeout: float = 10.0) -> list[dict]:
    """Probe the hosts the Spotify path actually needs, through the configured proxy."""
    import httpx

    from .network import httpx_options

    register_secrets(settings)
    results = []
    options = httpx_options(settings or {})
    for host in ("https://open.spotify.com/", "https://music.youtube.com/", "https://www.youtube.com/"):
        row = {"url": host, "ok": False, "detail": ""}
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, **options) as client:
                response = client.get(host, headers={"User-Agent": "NDM/diagnostics"})
            row["status"] = response.status_code
            row["ok"] = response.status_code < 500
            row["detail"] = f"HTTP {response.status_code}"
        except Exception as exc:
            row["detail"] = redact(f"{type(exc).__name__}: {exc}")[:200]
            row["hint"] = diagnose(exc)
        results.append(row)
    return results


def tail(data_dir: Path, lines: int = 60) -> str:
    """Last N lines of the log, for `ndm logs` and the desktop dialog."""
    path = log_path(data_dir)
    if not path.is_file():
        return f"No log yet at {path}"
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return "".join(handle.readlines()[-max(1, lines):])
    except OSError as exc:
        return f"Could not read {path}: {exc}"
