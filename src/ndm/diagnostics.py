"""Readable failure diagnostics: a redacted log file, upstream logs, and network hints.

Download failures used to surface only a short masked message, because yt-dlp was
created with ``quiet``/``no_warnings`` and spotDL's logger was pinned to ERROR.
Attaching a logger object to yt-dlp bypasses both switches, so the real network
warnings reach this module instead of being discarded.

Everything written here passes a redaction filter: proxy credentials, cookie
values, and Authorization headers never reach the log file.
"""
from __future__ import annotations

import functools
import hashlib
import logging
import logging.handlers
import os
import platform
import re
import shutil
import sys
import time
import traceback
from pathlib import Path

LOG_FILENAME = "ndm.log"
# spotDL logs its per-candidate scoring at a custom MATCH level *below* DEBUG,
# so a DEBUG handler still discards it. Capture it explicitly: the candidate
# scores are the whole point of a matching trace.
_MATCH_LEVEL = 5
logging.addLevelName(_MATCH_LEVEL, "MATCH")
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


def setup_logging(data_dir: Path, *, debug: bool | None = None, rotate: bool = True) -> Path:
    """Attach a redacted file handler to the root logger (idempotent).

    Only the daemon may rotate this file. RotatingFileHandler keeps its own
    size counter and renames the file on rollover, so a second process (a CLI
    command) rotating the same path swaps the inode underneath the daemon:
    the daemon keeps writing to the now-renamed file and its entries vanish
    from `ndm logs`. Non-owning processes append without rotating.
    """
    global _CONFIGURED
    path = log_path(data_dir)
    if _CONFIGURED == path:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    verbose = debug_enabled() if debug is None else debug
    if rotate:
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8", delay=False)
    else:
        handler = logging.FileHandler(path, mode="a", encoding="utf-8", delay=False)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"))
    handler.addFilter(_RedactingFilter())
    handler.addFilter(_UpstreamScoringFilter())
    # The file always keeps upstream's matching detail (MATCH=5 and DEBUG).
    # NDM's own debug stays opt-in via the root level below.
    handler.setLevel(_MATCH_LEVEL)
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
    # Matching detail is always recorded, because a failed match is otherwise
    # indistinguishable from a failed transfer.
    _configure_spotdl_level()
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _CONFIGURED = path
    return path


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def _configure_spotdl_level() -> None:
    # The scoring filter lives on the handler: a filter attached to the "spotdl"
    # logger would not see records propagated up from spotdl.utils.matching.
    logging.getLogger("spotdl").setLevel(_MATCH_LEVEL)


class _UpstreamScoringFilter(logging.Filter):
    """Drop spotDL's per-candidate score arithmetic unless NDM_DEBUG=1.

    spotDL emits ~25 MATCH lines for every candidate it considers, so a playlist
    produced tens of thousands of lines and pushed the actual transfer result out
    of both `ndm logs` and the rotating file. NDM already logs the candidate list
    and the winning score itself, so the arithmetic is only worth keeping when
    the user explicitly asks for verbose output.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != _MATCH_LEVEL or not record.name.startswith("spotdl"):
            return True
        return debug_enabled()


def configure_upstream_logging() -> None:
    """Keep spotDL's matching detail available in the log instead of discarding it."""
    _configure_spotdl_level()


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


def detail_text(exc: BaseException) -> str:
    """Full text of a failure, including causes hidden in custom attributes.

    Some upstream libraries keep the real reason outside the exception chain:
    spotapi's ``ParentException`` raises ``RequestError("Failed to complete
    request.", error=<real cause>)`` with no ``__cause__``, so reading only
    ``str(exc)`` loses the network error entirely.
    """
    parts: list[str] = []
    for item in exception_chain(exc):
        parts.append(f"{type(item).__name__}: {item}")
        for attribute in ("error", "reason", "msg"):
            value = getattr(item, attribute, None)
            if isinstance(value, BaseException):
                parts.append(f"{type(value).__name__}: {value}")
            elif isinstance(value, str) and value and value not in parts:
                parts.append(value)
    return redact(" ".join(dict.fromkeys(parts)))


def log_failure(logger: logging.Logger, stage: str, exc: BaseException,
                settings: dict | None = None, **fields) -> None:
    """Write the full, redacted failure chain plus the stage that produced it."""
    context = " ".join(f"{k}={redact(v)}" for k, v in fields.items() if v not in (None, ""))
    logger.error("FAILED at %s%s: %s: %s", stage, f" ({context})" if context else "",
                 type(exc).__name__, redact(exc))
    for depth, item in enumerate(exception_chain(exc)):
        logger.error("  cause[%d] %s: %s", depth, type(item).__name__, redact(item))
        hidden = getattr(item, "error", None)
        if hidden not in (None, "") and str(hidden) != str(item):
            logger.error("  cause[%d].error: %s", depth, redact(hidden))
    logger.debug("Traceback for %s:\n%s", stage,
                 redact("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))))
    hint = config_hint(settings, detail_text(exc)) or diagnose(exc)
    if hint:
        logger.error("  hint: %s", hint)


_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)cookie(s)? (file|database|profile)|could not (read|find|extract).*cookie|CookieLoadError"),
     "Browser cookies could not be read. In Options → Capture, set Use cookies from to Off, or select a browser with a readable signed-in profile."),
    (re.compile(r"(?i)sign in to confirm|not a bot|confirm you're not a bot|account.*terminated"),
     "YouTube is asking for a sign-in/bot check. Sign in to YouTube in Firefox or Chrome, select that browser under Options → Capture → Use cookies from, then retry."),
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
    # A SOCKS tunnel that opens but then stalls during the TLS handshake is a
    # *working* proxy carrying a blocked/dead upstream, not bad proxy settings.
    # Match it before the generic proxy rule: "SocksHTTPSConnectionPool" appears
    # in every proxied requests error and would otherwise mask the real cause.
    (re.compile(r"(?i)handshake operation timed out|handshake timed out"),
     "The proxy connected, but the TLS handshake through it never completed. The proxy itself is "
     "reachable; its upstream exit is blocked, overloaded, or dead. Switch to another proxy/server "
     "or VPN exit, then retry."),
    # Genuine proxy-layer failures only. Do not match the bare word "socks":
    # it is part of the pool class name in every proxied requests/urllib3 error.
    (re.compile(r"(?i)cannot connect to proxy|unable to connect to proxy|proxyconnectionerror"
                r"|generalproxyerror|socks5?auth|socks[45]error|proxy authentication|407"
                r"|tunnel connection failed|cannot connect to socks|invalid proxy|proxyerror"),
     "The proxy refused or failed the connection. Verify the proxy URL, port, and credentials in Options → Network."),
    (re.compile(r"(?i)getaddrinfo|name or service not known|temporary failure in name resolution"
                r"|nodename nor servname|failed to resolve|nameresolutionerror|\bdns\b"),
     "DNS lookup failed. Check your connection/VPN. With a SOCKS proxy, enable remote DNS "
     "(Options → Network) so the proxy resolves the hostname instead of this machine."),
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
    text = redact(exc if isinstance(exc, str) else detail_text(exc))
    for pattern, hint in _HINTS:
        if pattern.search(text):
            return hint
    return ""


def failure_message(exc: BaseException, *, detail: str = "", prefix: str = "",
                    limit: int = 460, settings: dict | None = None) -> str:
    """Build a queue-row message that survives safe_error's 500-char truncation.

    The actionable hint goes first: yt-dlp's raw text is often longer than the
    whole budget, so putting the hint last would hide the only useful sentence.
    """
    def tidy(text: str) -> str:
        # yt-dlp repeats the cause inside its own message; keep the leading part.
        text = redact(text).split(" (caused by ")[0].split("; please report this issue")[0].strip()
        return text[7:].strip() if text.startswith("ERROR: ") else text

    hint = config_hint(settings, detail_text(exc)) or diagnose(detail or exc)
    raw = tidy(str(exc)) or type(exc).__name__
    # A generic wrapper ("Failed to complete request.") hides the real cause in
    # an attribute or a chained exception. Prefer that text for the visible row.
    hidden = tidy(detail_text(exc))
    if len(raw) < 60 and hidden and hidden != raw and len(hidden) > len(raw):
        raw = hidden
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


_NETWORK_FAILURE = re.compile(
    r"(?i)timed out|timeout|handshake|failed to resolve|getaddrinfo|name or service not known"
    r"|connection refused|network is unreachable|connection reset|tls/ssl"
    # A poisoned local lookup often surfaces as a TLS record cut short rather than
    # a timeout. Without these, the same misconfiguration produced two different
    # hints depending on which way the connection happened to fail.
    r"|sslerror|ssleoferror|unexpected_eof_while_reading|eof occurred in violation of protocol"
    r"|max retries exceeded|wrong version number|unknown ca|certificate verify failed")


def config_hint(settings: dict | None, exc: BaseException | str = "") -> str:
    """Advice that depends on the user's settings, not just the error text.

    With a SOCKS proxy and remote DNS off, requests resolves hostnames on this
    machine before handing the proxy an IP. On a censored/poisoned resolver that
    produces a handshake timeout that looks like a dead proxy exit, so name the
    actual setting instead of sending the user to replace a working proxy.
    """
    data = settings or {}
    if not data.get("proxy_enabled"):
        return ""
    url = str(data.get("proxy_url") or "")
    if not url.lower().startswith(("socks5://", "socks://")):
        return ""
    if data.get("proxy_remote_dns") is False and (not exc or _NETWORK_FAILURE.search(str(exc))):
        return ("Remote DNS is OFF, so hostnames are resolved on this machine before the proxy "
                "sees them. Enable Options → Network → remote DNS (socks5h) and retry.")
    return ""


@functools.lru_cache(maxsize=1)
def code_fingerprint() -> str:
    """Identify the source this process loaded, so a stale daemon is detectable.

    Cached for the process lifetime on purpose: the daemon must keep reporting
    the code it actually imported at startup. Recomputing from disk on every
    request would make an old daemon read the newly pulled files and falsely
    report itself as up to date.

    The desktop only replaced a daemon when SETTINGS_SCHEMA_VERSION changed, so
    a `git pull` that altered behaviour but not the settings shape left the old
    process serving requests: new code on disk, old code in memory, and log
    output that silently matched neither. This is especially easy to hit with
    `uv run ndm` from a checkout, where the CLI is fresh on every invocation but
    the daemon is long-lived.

    Hash the package's .py contents (not mtimes, which a checkout rewrites) and
    fall back to the version string if the source is unavailable, e.g. when
    frozen or installed as a zip.
    """
    try:
        root = Path(__file__).resolve().parent
        digest = hashlib.sha256()
        files = sorted(root.glob("*.py"))
        if not files:
            raise OSError("no source files")
        for path in files:
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()[:16]
    except Exception:
        return f"version-{os.environ.get('NDM_VERSION', 'unknown')}"


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
        "proxy_remote_dns": bool(data.get("proxy_remote_dns", True)),
        "cookie_browser": data.get("cookie_browser") or ("firefox" if data.get("firefox_cookies") else "none"),
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
    """Probe the hosts the Spotify path needs, on both HTTP stacks NDM uses.

    httpx (metadata/doctor) and requests+PySocks (spotDL's YouTube Music client)
    do not treat ``socks5://`` identically: httpx always sends the hostname to
    the proxy, while requests resolves it locally first. With remote DNS off on
    a censored network, httpx can succeed while matching times out, so probing
    only httpx would report a false "OK".
    """
    import httpx

    from .network import httpx_options, proxy_url, requests_proxies

    register_secrets(settings)
    results = []
    options = httpx_options(settings or {})
    proxies = requests_proxies(settings or {})
    # Name the proxy in the report so a failing tunnel is not mistaken for a
    # direct-connection problem. Credentials stay redacted.
    via = redact(proxy_url(settings or {}))
    for host in ("https://open.spotify.com/", "https://music.youtube.com/", "https://www.youtube.com/"):
        for stack in ("httpx", "requests"):
            row = {"url": host, "ok": False, "detail": "", "via": via or "direct", "stack": stack}
            try:
                if stack == "httpx":
                    with httpx.Client(timeout=timeout, follow_redirects=True, **options) as client:
                        response = client.get(host, headers={"User-Agent": "NDM/diagnostics"})
                    status = response.status_code
                else:
                    import requests

                    response = requests.get(host, proxies=proxies, timeout=timeout,
                                            headers={"User-Agent": "NDM/diagnostics"},
                                            allow_redirects=True)
                    status = response.status_code
                    response.close()
                row["status"] = status
                row["ok"] = status < 500
                row["detail"] = f"HTTP {status}"
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


class Tracker:
    """Stage-by-stage progress for one job, with elapsed timings.

    Every line carries the job id so a single track can be followed through
    queueing, matching, transfer, conversion, and tagging even when several
    downloads interleave. Byte progress is throttled so a fast transfer cannot
    flood the log, but phase changes and the final totals always print.
    """

    def __init__(self, logger: logging.Logger, job: str, *, interval: float = 2.0) -> None:
        self.log = logger
        self.job = job
        self.interval = interval
        self.started = time.monotonic()
        self._stage_at = self.started
        self._last_progress = 0.0
        self._last_phase = ""
        self._progress_phase = ""
        self._last_bytes = -1

    def _emit(self, level: int, message: str, *args) -> None:
        self.log.log(level, "[%s] " + message, self.job, *args)

    def stage(self, name: str, /, **fields) -> None:
        """Enter a stage. Logs the duration of the stage that just finished."""
        if name == self._last_phase:
            # The engine and the progress callback can announce the same phase.
            return
        now = time.monotonic()
        extra = " ".join(f"{k}={redact(v)}" for k, v in fields.items() if v not in (None, ""))
        self._emit(logging.INFO, "stage=%s (+%.2fs total=%.2fs)%s", name,
                   now - self._stage_at, now - self.started, f" {extra}" if extra else "")
        self._stage_at = now
        self._last_phase = name

    def detail(self, message: str, *args) -> None:
        """Always-on informational detail within a stage."""
        self._emit(logging.INFO, message, *args)

    def debug(self, message: str, *args) -> None:
        self._emit(logging.DEBUG, message, *args)

    def progress(self, downloaded, total, phase: str = "", /, *, force: bool = False) -> None:
        """Throttled byte progress; a phase change always emits."""
        now = time.monotonic()
        changed = phase != self._progress_phase
        if not force and not changed and now - self._last_progress < self.interval:
            return
        if not force and not changed and downloaded == self._last_bytes:
            return
        self._last_progress = now
        self._last_bytes = downloaded if downloaded is not None else -1
        self._progress_phase = phase
        elapsed = max(0.01, now - self.started)
        rate = (downloaded or 0) / elapsed
        percent = f"{(downloaded or 0) / total * 100:.1f}%" if total else "n/a"
        self._emit(logging.INFO, "progress phase=%s bytes=%s/%s (%s) avg=%s/s",
                   phase or "-", downloaded if downloaded is not None else "?",
                   total if total else "?", percent, _bytes(rate))

    def done(self, **fields) -> None:
        extra = " ".join(f"{k}={redact(v)}" for k, v in fields.items() if v not in (None, ""))
        self._emit(logging.INFO, "DONE in %.2fs%s", time.monotonic() - self.started,
                   f" {extra}" if extra else "")

    def failed(self, exc: BaseException, /, **fields) -> None:
        self._emit(logging.ERROR, "FAILED after %.2fs at stage=%s", time.monotonic() - self.started,
                   self._last_phase or "-")
        log_failure(self.log, f"job {self.job}", exc, **fields)


def _bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024 or unit == "GB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"
