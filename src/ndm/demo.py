"""Real local files for the hosted preview. Never presented as external downloads."""
from __future__ import annotations

import math
import random
import shutil
import threading
import time
import wave
import zipfile
from array import array
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse

SPECS = {
    "studio-assets.zip": (96_000_000, "Studio assets — November.zip", "archive", "active"),
    "font-collection.zip": (48_000_000, "Open fonts — collection.zip", "archive", "active"),
    "field-recording.wav": (24_000_000, "Field recording — ambient.wav", "audio", "active"),
    "project-source.zip": (16_000_000, "Weekend project — source.zip", "archive", "queued"),
    "textures.zip": (12_000_000, "Sketchbook — textures.zip", "archive", "paused"),
    "quick-start.pdf": (0, "NDM — quick start.pdf", "document", "completed"),
    "cover.svg": (0, "Quiet waves — cover.svg", "image", "completed"),
    "ambient-preview.wav": (700_000, "Ambient tones — preview.wav", "audio", "completed"),
}
CHUNK = 64 * 1024
RATE = 350_000  # bytes per second, shared across all segments of each sample


def make_file(path: Path, key: str, size: int) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if key.endswith(".zip"):
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("README.txt", "NDM local sample archive.\nThis is synthetic demo data, not a third-party creative asset.\nTransferred by the real segmented download engine.\n")
            archive.writestr("cover.svg", '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600"><rect width="800" height="600" fill="#7856e8"/><circle cx="400" cy="300" r="180" fill="#b9a7f5"/><text x="400" y="320" text-anchor="middle" font-family="sans-serif" font-size="64" fill="white">ndm</text></svg>')
            with archive.open("sample-data.bin", "w") as handle:
                remaining = max(0, size - 1200)
                rng = random.Random(key)
                while remaining:
                    chunk = rng.randbytes(min(CHUNK, remaining))
                    handle.write(chunk)
                    remaining -= len(chunk)
    elif key.endswith(".wav"):
        frames = array("h")
        count = size // 2 if size else 100_000
        for i in range(count):
            t = i / 22050
            envelope = min(1, t / 2, (count / 22050 - t) / 2)
            value = math.sin(2 * math.pi * 220 * t) + 0.35 * math.sin(2 * math.pi * 330 * t)
            frames.append(int(max(-1, min(1, envelope * value / 2)) * 18000))
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(22050)
            handle.writeframes(frames.tobytes())
    elif key.endswith(".pdf"):
        text = b"BT /F1 28 Tf 60 730 Td (Welcome to NDM.) Tj 0 -45 Td /F1 14 Tf (Your downloads, in flow.) Tj 0 -35 Td (This is a real local sample PDF.) Tj 0 -30 Td (Add a direct link to start your own download.) Tj ET"
        objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                   b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
                   b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                   b"<< /Length " + str(len(text)).encode() + b" >>\nstream\n" + text + b"\nendstream"]
        content = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for i, obj in enumerate(objects, 1):
            offsets.append(len(content))
            content.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
        xref = len(content)
        content.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
        for offset in offsets[1:]:
            content.extend(f"{offset:010d} 00000 n \n".encode())
        content.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
        path.write_bytes(content)
    else:
        path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800" viewBox="0 0 1200 800"><rect width="1200" height="800" fill="#eee8ff"/><circle cx="850" cy="240" r="180" fill="#d4c5ff"/><path d="M0 550 Q300 250 600 550 T1200 550 V800 H0Z" fill="#7856e8"/><path d="M0 650 Q300 350 600 650 T1200 650 V800 H0Z" fill="#5c3bc4"/><text x="90" y="170" font-family="sans-serif" font-size="64" fill="#302449">Quiet waves.</text><text x="95" y="215" font-family="sans-serif" font-size="22" fill="#665780">An original NDM sample cover.</text></svg>')


def seed_demo(manager, port: int) -> None:
    if manager.store.get_meta("demo_seeded"):
        return
    directory = manager.store.directory / "fixtures"
    # Workers must not race the HTTP server's startup while the queue is seeded.
    manager.settings.engine_running = False
    for key, (size, name, category, status) in SPECS.items():
        path = directory / key
        make_file(path, key, size)
        task = manager.add(url=f"http://127.0.0.1:{port}/demo/{key}", filename=name,
                           category=category, source="demo", authorized=True,
                           start_paused=status in {"paused", "completed", "queued"})
        if status == "completed":
            current = manager.tasks[task["id"]]
            shutil.copyfile(path, current["path"])
            current.update(status="completed", size=path.stat().st_size, downloaded=path.stat().st_size,
                           completed_at=time.time(), mime="application/octet-stream")
        elif status != "paused":
            manager.action(task["id"], "resume")
        # Queue the fourth file behind the three concurrent samples.
        if status == "queued":
            manager.tasks[task["id"]]["status"] = "queued"
        manager.store.save_downloads([task["id"]], manager.tasks)
    manager.settings.engine_running = True
    manager.store.set_meta("demo_seeded", True)
    manager.store.event("Your sample workspace is ready", "Real local files. Add your own links whenever you're ready.", "success")


def register_demo(app, port: int) -> None:
    manager = app.state.manager
    directory = manager.store.directory / "fixtures"
    clocks = {key: [threading.Lock(), 0.0] for key in SPECS}

    @app.get("/demo/{key}")
    def sample(key: str, request: Request):
        if key not in SPECS:
            raise HTTPException(404, "Sample not found.")
        path = directory / key
        size = path.stat().st_size
        start, end = 0, size - 1
        range_header = request.headers.get("range")
        if range_header:
            import re
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if not match or not any(match.groups()):
                raise HTTPException(416, "Invalid range.")
            if match.group(1):
                start = int(match.group(1))
                end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
            else:
                start = max(0, size - int(match.group(2)))
            if start >= size or start > end:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        length = end - start + 1
        ascii_name = SPECS[key][1].encode("ascii", "replace").decode()
        headers = {"Accept-Ranges": "bytes", "Content-Length": str(length),
                   "Content-Disposition": f'attachment; filename="{ascii_name}"',
                   "ETag": f'"{key}-v1"', "Cache-Control": "no-store"}
        if range_header:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        mime = "audio/wav" if key.endswith(".wav") else "application/zip" if key.endswith(".zip") else "application/pdf" if key.endswith(".pdf") else "image/svg+xml"

        def stream():
            clock, next_send = clocks[key]
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining:
                    chunk = handle.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    with clock:
                        now = time.monotonic()
                        delay = max(0, next_send - now)
                        next_send = max(now, next_send) + len(chunk) / RATE
                        clocks[key][1] = next_send
                    if delay:
                        time.sleep(delay)
                    remaining -= len(chunk)
                    yield chunk
        return StreamingResponse(stream(), status_code=206 if range_header else 200, headers=headers, media_type=mime)
