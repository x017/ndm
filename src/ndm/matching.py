"""Find the YouTube Music source for a Spotify track, without spotDL's provider.

NDM calls ytmusicapi directly and scores candidates itself. The upstream
provider wraps failures in ``AudioProviderError`` and hides candidate inspection
inside its own yt-dlp instance; owning the search keeps every network error and
every candidate visible in NDM's diagnostics log.

Scoring is deliberately explicit rather than a port of upstream's ~970 lines of
accumulated fixups. Signals, in order of weight: normalized title, artists,
duration, then album/verified bonuses, with penalties for versions the user did
not ask for (live, cover, sped up...). An ISRC hit short-circuits the search.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import partial

from .diagnostics import get_logger, redact
from .models import SpotifyCancelled

log = get_logger("ndm.matching")

# Versions that are usually NOT what a Spotify album track should resolve to,
# unless the Spotify title itself asks for them.
VERSION_WORDS = (
    "live", "cover", "karaoke", "instrumental", "sped up", "speed up", "slowed",
    "reverb", "nightcore", "remix", "acoustic", "8d", "reaction", "tutorial",
)
_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_FEAT = re.compile(r"\b(feat|ft|with|prod)\.?\s.*$")
_NOISE = re.compile(
    r"\b(official|music|video|audio|lyric|lyrics|hd|hq|mv|visualizer|topic|explicit|clean)\b"
)
_PUNCT = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")
ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")
_VIEW_COUNT = re.compile(r"^[\d.,]+\s*([KMB]|million|billion|thousand)?\s*(views|plays)?$", re.I)


def normalize(value: str) -> str:
    """Fold a title/artist to comparable text: no accents, brackets, or noise."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = _BRACKETS.sub(" ", text)
    text = _FEAT.sub(" ", text)
    text = text.replace("&", " and ")
    text = _PUNCT.sub(" ", text)
    text = _NOISE.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def _ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 100.0
    return SequenceMatcher(None, left, right).ratio() * 100.0


def parse_duration(value) -> float:
    """Accept '3:45', '1:02:03', or a number of seconds."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        parts = [float(p) for p in text.split(":")]
    except ValueError:
        return 0.0
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


@dataclass
class Candidate:
    video_id: str
    title: str
    artists: list[str]
    duration: float
    verified: bool
    album: str = ""
    explicit: bool = False
    views: str = ""
    url: str = ""
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)


def candidates_from(raw: list[dict]) -> list[Candidate]:
    """Convert ytmusicapi search rows into candidates, dropping unusable ones."""
    results: list[Candidate] = []
    for row in raw or []:
        if not isinstance(row, dict) or not row.get("videoId"):
            continue
        artists = [
            str(a["name"]) for a in (row.get("artists") or [])
            if isinstance(a, dict) and a.get("name") and not _VIEW_COUNT.match(str(a["name"]))
        ]
        if not artists:
            continue
        verified = row.get("resultType") == "song"
        album = row.get("album") or {}
        results.append(Candidate(
            video_id=row["videoId"],
            title=str(row.get("title") or ""),
            artists=artists,
            duration=parse_duration(row.get("duration_seconds") or row.get("duration")),
            verified=verified,
            album=str(album.get("name") or "") if isinstance(album, dict) else "",
            explicit=bool(row.get("isExplicit")),
            views=str(row.get("views") or ""),
            url=f"https://{'music' if verified else 'www'}.youtube.com/watch?v={row['videoId']}",
        ))
    return results


def score(song: dict, candidate: Candidate) -> Candidate:
    """Score one candidate 0-100 and record why, for the diagnostics log."""
    reasons: list[str] = []
    wanted_title = normalize(song.get("name"))
    got_title = normalize(candidate.title)
    title = _ratio(wanted_title, got_title)
    reasons.append(f"title={title:.0f}")

    wanted_artists = [normalize(a) for a in (song.get("artists") or [song.get("artist")]) if a]
    got_artists = [normalize(a) for a in candidate.artists if a]
    if wanted_artists and got_artists:
        # Each Spotify artist scores against its best counterpart; averaging
        # keeps a lone matching artist from carrying a wrong collaboration.
        per_artist = [max(_ratio(w, g) for g in got_artists) for w in wanted_artists]
        artist = sum(per_artist) / len(per_artist)
    else:
        artist = 0.0
    reasons.append(f"artist={artist:.0f}")

    wanted_duration = parse_duration(song.get("duration"))
    duration = 100.0
    if wanted_duration and candidate.duration:
        delta = abs(wanted_duration - candidate.duration)
        if delta <= 2:
            duration = 100.0
        elif delta <= 10:
            duration = 100.0 - (delta - 2) * 5      # 100 -> 60
        elif delta <= 30:
            duration = 60.0 - (delta - 10) * 2.5    # 60 -> 10
        else:
            duration = 0.0
        reasons.append(f"duration={duration:.0f}(Δ{delta:.0f}s)")
    else:
        duration = 50.0
        reasons.append("duration=unknown")

    total = title * 0.40 + artist * 0.35 + duration * 0.20
    if candidate.verified:
        total += 5.0
        reasons.append("verified+5")
    wanted_album = normalize(song.get("album_name"))
    if wanted_album and candidate.album and _ratio(wanted_album, normalize(candidate.album)) > 85:
        total += 3.0
        reasons.append("album+3")

    # Penalize versions the Spotify title did not ask for. Test the RAW candidate
    # title: normalize() strips bracketed text, which is exactly where "(Live)"
    # and "[Sped Up]" live, so checking the normalized form would miss them.
    raw_title = str(song.get("name") or "").lower()
    raw_candidate = str(candidate.title or "").lower()
    unwanted = [w for w in VERSION_WORDS if w in raw_candidate and w not in raw_title]
    if unwanted:
        total -= 25.0 * len(unwanted)
        reasons.append(f"version-{'/'.join(unwanted)}")
    if duration == 0.0:
        total -= 20.0
        reasons.append("length-mismatch")

    candidate.score = max(0.0, min(100.0, total))
    candidate.reasons = reasons
    return candidate


def create_client(settings: dict | None = None):
    """A YTMusic client honouring NDM's proxy, opt-in cookies, and a timeout."""
    import requests
    from ytmusicapi import YTMusic

    from .network import firefox_cookie_jar, requests_proxies

    data = settings or {}
    jar = firefox_cookie_jar(data)
    proxies = requests_proxies(data)
    session = requests.Session()
    session.request = partial(session.request, timeout=30)
    session.trust_env = not proxies
    if jar is not None:
        # Requests applies domain/path/secure matching. Never flatten to a header.
        session.cookies.update(jar)
    return YTMusic(language="de", proxies=proxies, requests_session=session)


def _search(client, query: str, filter_: str | None, stop) -> list[Candidate]:
    if stop is not None and stop.is_set():
        raise SpotifyCancelled()
    kwargs = {"filter": filter_, "limit": 20, "ignore_spelling": True} if filter_ else {"limit": 20}
    log.debug("YTM search %r filter=%s", query, filter_)
    return candidates_from(client.search(query, **kwargs))


def candidates_from_ytdlp(entries: list[dict]) -> list[Candidate]:
    """Convert yt-dlp search rows into candidates.

    A flat YouTube search has no artist array or song/video flag, so the channel
    name stands in for the artist and a "- Topic" channel (YouTube's auto-generated
    artist channels) is treated as the verified equivalent.
    """
    results: list[Candidate] = []
    for row in entries or []:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        channel = str(row.get("channel") or row.get("uploader") or "").strip()
        if not channel:
            continue
        artist = re.sub(r"\s*-\s*Topic$", "", channel).strip() or channel
        title = str(row.get("title") or "")
        # YouTube titles are usually "Artist - Title", while Spotify stores the
        # bare title. Strip a leading artist credit so the shared scorer compares
        # like with like; without this an exact match scored ~68 instead of ~100.
        for credit in {artist, channel}:
            prefix = re.compile(r"^\s*" + re.escape(credit) + r"\s*[-\u2013\u2014:]\s+", re.I)
            if prefix.search(title):
                title = prefix.sub("", title, count=1)
                break
        results.append(Candidate(
            video_id=str(row["id"]),
            title=title,
            artists=[artist],
            duration=float(row.get("duration") or 0),
            verified=channel.lower().endswith("- topic"),
            views=str(row.get("view_count") or ""),
            url=f"https://www.youtube.com/watch?v={row['id']}",
        ))
    return results


def search_ytdlp(query: str, *, settings: dict | None = None, stop=None, limit: int = 10) -> list[Candidate]:
    """Search YouTube through yt-dlp instead of the YouTube Music API.

    ytmusicapi talks to music.youtube.com over requests; when that endpoint is
    blocked or its visitor-id handshake stalls, matching dies even though yt-dlp
    can still reach YouTube and download. This uses the same stack as the media
    grabber, so a working grabber means a working search.
    """
    import yt_dlp

    from .diagnostics import YtdlpLogger, debug_enabled
    from .network import ytdlp_options

    if stop is not None and stop.is_set():
        raise SpotifyCancelled()
    options = {
        "quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True,
        "extract_flat": "in_playlist", "socket_timeout": 20, "extractor_retries": 1,
        "logger": YtdlpLogger("ndm.matching.ytdlp", "[yt-search]"),
        "verbose": debug_enabled(), **ytdlp_options(settings or {}),
    }
    target = f"ytsearch{max(1, limit)}:{query}"
    log.info("yt-dlp search %r", redact(query))
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(target, download=False)
    entries = list((info or {}).get("entries") or [])
    found = candidates_from_ytdlp(entries)
    log.info("yt-dlp search %r returned %d candidate(s)", redact(query), len(found))
    return found


def strip_artist_prefix(title: str, artists: list[str]) -> str:
    """Drop a leading "Artist - " credit from a YouTube title.

    The channel is often a VEVO/label account rather than the artist, so the
    converter cannot always do this; match against the wanted artists too.
    """
    for credit in artists:
        if not credit:
            continue
        prefix = re.compile(r"^\s*" + re.escape(str(credit)) + r"\s*[-\u2013\u2014:]\s+", re.I)
        if prefix.search(title):
            return prefix.sub("", title, count=1)
    return title


def find_match_ytdlp(song: dict, *, settings: dict | None = None, stop=None) -> dict:
    """Match a Spotify track using yt-dlp search and NDM's own scoring."""
    title = str(song.get("name") or "").strip()
    artists = [a for a in (song.get("artists") or [song.get("artist")]) if a]
    if not title or not artists:
        raise ValueError("This Spotify track is missing the title/artist metadata needed for matching.")
    query = f"{' '.join(artists)} {title}".strip()
    found = search_ytdlp(query, settings=settings, stop=stop)
    if not found:
        raise LookupError(f"No YouTube results for {', '.join(artists)} - {title}. "
                          "Check the track title/artist, or retry. Full log: ndm logs")
    for candidate in found:
        candidate.title = strip_artist_prefix(candidate.title, artists)
    ranked = sorted((score(song, c) for c in found), key=lambda c: c.score, reverse=True)
    for hit in ranked[:5]:
        log.info("yt-dlp candidate %s score=%.1f %s | %s", hit.url, hit.score,
                 hit.title, " ".join(hit.reasons))
    best = ranked[0]
    log.info("Best yt-dlp match %s score=%.1f (%s)", best.url, best.score, " ".join(best.reasons))
    return {"url": best.url, "score": best.score, "title": best.title,
            "artists": best.artists, "matched_by": "yt-dlp search"}


def find_match(song: dict, *, settings: dict | None = None, stop=None, client=None) -> dict:
    """Return {'url', 'score', 'title', 'artists'} for the best YouTube source."""
    title = str(song.get("name") or "").strip()
    artists = [a for a in (song.get("artists") or [song.get("artist")]) if a]
    if not title or not artists:
        raise ValueError("This Spotify track is missing the title/artist metadata needed for matching.")

    client = client if client is not None else create_client(settings)
    isrc = str(song.get("isrc") or "").strip().upper()

    # An ISRC is the strongest signal available; trust a verified hit whose
    # length agrees, otherwise fall through to a normal title/artist search.
    if isrc and ISRC_RE.match(isrc):
        hits = _search(client, isrc, "songs", stop)
        ranked = sorted((score(song, c) for c in hits), key=lambda c: c.score, reverse=True)
        for hit in ranked:
            log.info("ISRC %s candidate %s score=%.1f (%s)", isrc, hit.url, hit.score,
                     " ".join(hit.reasons))
        if ranked and ranked[0].verified and ranked[0].score >= 80:
            best = ranked[0]
            return {"url": best.url, "score": best.score, "title": best.title,
                    "artists": best.artists, "matched_by": "isrc"}

    query = f"{title} {' '.join(artists)}".strip()
    pool: dict[str, Candidate] = {}
    # "songs" first: album tracks are usually there and it is the accurate filter.
    for filter_ in ("songs", "videos"):
        found = _search(client, query, filter_, stop)
        log.info("Query %r filter=%s returned %d candidates", redact(query), filter_, len(found))
        for candidate in found:
            scored = score(song, candidate)
            log.debug("  %s score=%.1f %s | %s", scored.url, scored.score,
                      scored.title, " ".join(scored.reasons))
            previous = pool.get(scored.video_id)
            if previous is None or scored.score > previous.score:
                pool[scored.video_id] = scored
        ranked = sorted(pool.values(), key=lambda c: c.score, reverse=True)
        if ranked and ranked[0].verified and ranked[0].score >= 80:
            break

    if not pool:
        raise LookupError(f"No YouTube Music results for {', '.join(artists)} - {title}. "
                          "Check the track title/artist, or retry. Full log: ndm logs")
    best = max(pool.values(), key=lambda c: (c.score, c.verified))
    log.info("Best match %s score=%.1f (%s)", best.url, best.score, " ".join(best.reasons))
    return {"url": best.url, "score": best.score, "title": best.title,
            "artists": best.artists, "matched_by": "search"}
