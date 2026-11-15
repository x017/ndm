"""Write Spotify metadata and cover art straight into the converted audio file.

NDM owns this instead of calling spotDL's ``embed_metadata``. Upstream assigns
optional fields unconditionally, so a track without an ISRC/label/release date
raised ``Invalid MultiSpec data: None`` *after* the audio was already saved.
Here every field is written only when it has a value, so a missing field simply
produces no frame rather than a failed download.

Container details differ enough that each gets its own writer:
ID3 frames for MP3, iTunes atoms for M4A, Vorbis comments for FLAC/OGG/Opus.
"""
from __future__ import annotations

import base64
from pathlib import Path

from .diagnostics import get_logger, redact
from .models import SpotifyCancelled

log = get_logger("ndm.tagging")

VORBIS_EXTENSIONS = {".flac", ".ogg", ".opus"}
SUPPORTED_EXTENSIONS = VORBIS_EXTENSIONS | {".mp3", ".m4a"}

# iTunes freeform atoms. The com.apple.iTunes namespace is what MusicBrainz
# Picard and most taggers read, rather than a tool-specific one.
M4A_ISRC = "----:com.apple.iTunes:ISRC"
M4A_WEBSITE = "----:com.apple.iTunes:WOAS"


class TaggingError(Exception):
    """The audio file could not be tagged."""


def _text(value) -> str:
    """Normalize a metadata value to a non-empty string, or "" when absent."""
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def _number(value) -> int:
    """Coerce track/disc numbers, tolerating None and '3/12' strings."""
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(0, int(str(value).split("/")[0].strip()))
    except (TypeError, ValueError):
        return 0


def _artists(song: dict) -> list[str]:
    raw = song.get("artists")
    values = [_text(a) for a in raw if _text(a)] if isinstance(raw, (list, tuple)) else []
    if not values:
        single = _text(song.get("artist"))
        values = [single] if single else []
    return values or ["Unknown artist"]


def fields(song: dict) -> dict:
    """Reduce raw song metadata to the values worth writing.

    Empty/None values are dropped here so no writer has to guard against them.
    """
    artists = _artists(song)
    data = {
        "title": _text(song.get("name")) or "Unknown title",
        "artists": artists,
        "artist": _text(song.get("artist")) or artists[0],
        "album_artist": _text(song.get("album_artist")) or _text(song.get("artist")) or artists[0],
        "album": _text(song.get("album_name")),
        "date": _text(song.get("date")),
        "year": _text(song.get("year")),
        "genre": _text((song.get("genres") or [None])[0]).title(),
        "publisher": _text(song.get("publisher")),
        "copyright": _text(song.get("copyright_text")),
        "isrc": _text(song.get("isrc")),
        "website": _text(song.get("url")),
        "comment": _text(song.get("download_url")),
        "lyrics": _text(song.get("lyrics")),
        "cover_url": _text(song.get("cover_url")),
        "track_number": _number(song.get("track_number")),
        "tracks_count": _number(song.get("tracks_count")),
        "disc_number": _number(song.get("disc_number")),
        "disc_count": _number(song.get("disc_count")),
    }
    if song.get("explicit") is not None:
        data["explicit"] = bool(song.get("explicit"))
    popularity = _number(song.get("popularity"))
    if 0 < popularity <= 100:
        data["popularity"] = popularity
    return data


def fetch_cover(url: str, settings: dict | None = None, *, stop=None) -> bytes | None:
    """Download cover art through NDM's proxy. A failed cover never fails a track."""
    if not url:
        return None
    import httpx

    from .network import httpx_options

    try:
        with httpx.Client(timeout=15, follow_redirects=True, **httpx_options(settings or {})) as client:
            response = client.get(url)
            response.raise_for_status()
            data = response.content
        if stop is not None and stop.is_set():
            return None
        if not data:
            return None
        return data
    except Exception as exc:
        log.warning("Cover art unavailable (%s): %s", redact(url), redact(exc))
        return None


def _cover_mime(data: bytes) -> str:
    return "image/png" if data.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg"


def _tag_mp3(path: Path, data: dict, cover: bytes | None) -> None:
    from mutagen.id3 import (
        APIC,
        COMM,
        ID3,
        POPM,
        TALB,
        TCON,
        TCOP,
        TDRC,
        TENC,
        TIT2,
        TPE1,
        TPE2,
        TPOS,
        TRCK,
        TSRC,
        TYER,
        USLT,
        WOAS,
        ID3NoHeaderError,
    )

    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()

    def put(frame):
        tags.delall(frame.FrameID)
        tags.add(frame)

    put(TIT2(encoding=3, text=data["title"]))
    put(TPE1(encoding=3, text=data["artists"]))
    put(TPE2(encoding=3, text=data["album_artist"]))
    for key, frame in (("album", TALB), ("date", TDRC), ("genre", TCON),
                       ("publisher", TENC), ("copyright", TCOP), ("isrc", TSRC)):
        if data.get(key):
            put(frame(encoding=3, text=data[key]))
    if data.get("year"):
        put(TYER(encoding=3, text=data["year"]))
    if data["track_number"] or data["tracks_count"]:
        put(TRCK(encoding=3, text=f"{data['track_number']}/{data['tracks_count']}"
                 if data["tracks_count"] else str(data["track_number"])))
    if data["disc_number"] or data["disc_count"]:
        put(TPOS(encoding=3, text=f"{data['disc_number']}/{data['disc_count']}"
                 if data["disc_count"] else str(data["disc_number"])))
    if data["website"]:
        tags.delall("WOAS")
        tags.add(WOAS(encoding=3, url=data["website"]))
    if data["comment"]:
        tags.delall("COMM")
        tags.add(COMM(encoding=3, lang="eng", desc="", text=data["comment"]))
    if data["lyrics"]:
        tags.delall("USLT")
        tags.add(USLT(encoding=3, lang="eng", desc="", text=data["lyrics"]))
    if data.get("popularity"):
        tags.delall("POPM")
        # ID3 popularimeter is a 0-255 byte; Spotify reports 0-100.
        tags.add(POPM(email="ndm", rating=round(data["popularity"] * 255 / 100), count=0))
    if cover:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=_cover_mime(cover), type=3, desc="Cover", data=cover))
    # v2.3 is what upstream wrote and what car/phone head units still read.
    tags.save(str(path), v2_version=3)


def _tag_m4a(path: Path, data: dict, cover: bytes | None) -> None:
    from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

    audio = MP4(str(path))
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags

    def put(key, value):
        tags[key] = value

    put("\xa9nam", [data["title"]])
    put("\xa9ART", data["artists"])
    put("aART", [data["album_artist"]])
    for key, atom in (("album", "\xa9alb"), ("date", "\xa9day"), ("genre", "\xa9gen"),
                      ("publisher", "\xa9too"), ("copyright", "cprt"),
                      ("comment", "\xa9cmt"), ("lyrics", "\xa9lyr")):
        if data.get(key):
            put(atom, [data[key]])
    if data["track_number"] or data["tracks_count"]:
        put("trkn", [(data["track_number"], data["tracks_count"])])
    if data["disc_number"] or data["disc_count"]:
        put("disk", [(data["disc_number"], data["disc_count"])])
    if data.get("explicit") is not None:
        put("rtng", [4 if data["explicit"] else 2])
    if data["isrc"]:
        put(M4A_ISRC, [MP4FreeForm(data["isrc"].encode("utf-8"))])
    if data["website"]:
        put(M4A_WEBSITE, [MP4FreeForm(data["website"].encode("utf-8"))])
    if cover:
        fmt = MP4Cover.FORMAT_PNG if _cover_mime(cover) == "image/png" else MP4Cover.FORMAT_JPEG
        put("covr", [MP4Cover(cover, imageformat=fmt)])
    audio.save()


def _tag_vorbis(path: Path, data: dict, cover: bytes | None, extension: str) -> None:
    import mutagen
    from mutagen.flac import FLAC, Picture

    audio = mutagen.File(str(path))
    if audio is None:
        raise TaggingError(f"Mutagen could not read {path.name} as {extension.lstrip('.')}")

    audio["title"] = data["title"]
    audio["artist"] = data["artists"]
    audio["albumartist"] = data["album_artist"]
    for key in ("album", "date", "genre", "publisher", "copyright", "isrc", "lyrics"):
        if data.get(key):
            audio[key] = data[key]
    if data["website"]:
        audio["website"] = data["website"]
    if data["comment"]:
        audio["comment"] = data["comment"]
    for key, value in (("tracknumber", data["track_number"]), ("tracktotal", data["tracks_count"]),
                       ("discnumber", data["disc_number"]), ("disctotal", data["disc_count"])):
        if value:
            audio[key] = str(value)

    if cover:
        picture = Picture()
        picture.type = 3
        picture.desc = "Cover"
        picture.mime = _cover_mime(cover)
        picture.data = cover
        if extension == ".flac" and isinstance(audio, FLAC):
            audio.clear_pictures()
            audio.add_picture(picture)
        else:
            # Ogg/Opus carry artwork as a base64 FLAC picture block in a comment.
            audio["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]
    audio.save()


def write_tags(path: Path, song: dict, *, settings: dict | None = None, stop=None) -> None:
    """Tag a converted file in place. Raises TaggingError on an unreadable file."""
    if stop is not None and stop.is_set():
        raise SpotifyCancelled()
    path = Path(path)
    extension = path.suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise TaggingError(f"Unsupported audio format for tagging: {extension or path.name}")

    data = fields(song)
    cover = fetch_cover(data["cover_url"], settings, stop=stop)
    if stop is not None and stop.is_set():
        raise SpotifyCancelled()

    log.info("Tagging %s (title=%r artists=%s isrc=%s cover=%s)", path.name, data["title"],
             ", ".join(data["artists"]), data["isrc"] or "-", f"{len(cover)}B" if cover else "none")
    try:
        if extension == ".mp3":
            _tag_mp3(path, data, cover)
        elif extension == ".m4a":
            _tag_m4a(path, data, cover)
        else:
            _tag_vorbis(path, data, cover, extension)
    except TaggingError:
        raise
    except Exception as exc:
        raise TaggingError(f"{type(exc).__name__}: {redact(exc)}") from exc
