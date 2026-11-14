"""Local API and optional browser companion. No browser-facing localhost URLs."""
from __future__ import annotations

import asyncio
import hmac
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .engine import DownloadManager
from .media import inspect_media, spotify_link
from .models import CaptureRequest, MediaRequest, NewDownload, SettingsPatch
from .store import Store


class MediaDownload(MediaRequest):
    format_id: str = "best"
    name: str = Field(default="media", max_length=180)
    category: str = "video"
    quality: str = "best"
    ext: str = Field(default="mp4", max_length=8)


class BulkAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[str] = Field(max_length=200)
    action: str


def create_app(manager: DownloadManager, *, static_dir: Path | None = None, demo_port: int | None = None) -> FastAPI:
    app = FastAPI(title="NDM", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.manager = manager
    token = manager.store.get_meta("capture_token")
    if not token:
        token = secrets.token_urlsafe(32)
        manager.store.set_meta("capture_token", token)
    static_dir = static_dir or Path(__file__).parent / "static"

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.method in {"POST", "PATCH", "PUT", "DELETE"} and request.url.path.startswith("/api/"):
            auth = request.headers.get("authorization", "")
            bearer = auth[7:] if auth.startswith("Bearer ") else ""
            trusted = hmac.compare_digest(bearer, token)
            if trusted and request.url.path != "/api/capture":
                return JSONResponse({"detail": "The capture token can only submit media captures."}, status_code=403)
            if not trusted and request.headers.get("x-ndm-client") != "1":
                return JSONResponse({"detail": "Missing NDM client header."}, status_code=403)
            origin = request.headers.get("origin")
            if origin and not trusted:
                expected_host = request.headers.get("x-forwarded-host", request.headers.get("host", "")).split(",")[0].strip()
                if urlsplit(origin).netloc != expected_host:
                    return JSONResponse({"detail": "Cross-origin changes are not allowed."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if not manager.hosted:
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def value_error(request: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def not_found(request: Request, exc: KeyError):
        return JSONResponse({"detail": str(exc.args[0]) if exc.args else "Not found."}, status_code=404)

    @app.exception_handler(RuntimeError)
    async def conflict(request: Request, exc: RuntimeError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "name": "ndm", "version": "0.1.0"}

    @app.get("/api/state")
    def state():
        result = manager.snapshot()
        result["daemon"]["demo"] = demo_port is not None
        return result

    @app.post("/api/downloads")
    def add(payload: NewDownload):
        if spotify_link(payload.url):
            raise ValueError("Open Spotify links in Spotify. To build an offline library, add audio you own or an authorized direct link.")
        return manager.add(**payload.model_dump())

    @app.post("/api/downloads/{task_id}/{action}")
    def action(task_id: str, action: str):
        return manager.action(task_id, action)

    @app.post("/api/bulk")
    def bulk(payload: BulkAction):
        results = []
        for task_id in payload.ids:
            try:
                manager.action(task_id, payload.action)
                results.append({"id": task_id, "ok": True})
            except (KeyError, ValueError) as exc:
                results.append({"id": task_id, "ok": False, "error": str(exc)})
        return {"results": results}

    @app.patch("/api/settings")
    def settings(payload: SettingsPatch):
        return manager.update_settings(payload).model_dump()

    @app.post("/api/media/inspect")
    async def media(payload: MediaRequest):
        if not payload.authorized and not spotify_link(payload.url):
            raise ValueError("Confirm that you own this media or have permission to save it.")
        return await asyncio.to_thread(inspect_media, payload.url, allow_private=manager.allow_private)

    @app.post("/api/media/download")
    def download_media(payload: MediaDownload):
        return manager.add_media(url=payload.url, format_id=payload.format_id, name=payload.name,
                                 category=payload.category, quality=payload.quality, ext=payload.ext,
                                 authorized=payload.authorized)

    @app.get("/api/capture-config")
    def capture_config():
        return {"token": token, "enabled": manager.settings.auto_capture,
                "endpoint": "/api/capture", "local_endpoint": "http://127.0.0.1:8765/api/capture"}

    @app.options("/api/capture")
    def capture_options(request: Request):
        origin = request.headers.get("origin", "")
        if not re.fullmatch(r"(?:chrome|moz)-extension://[a-p]{32}", origin):
            raise HTTPException(403, "Only the installed capture companion may use this endpoint.")
        return Response(status_code=204, headers={
            "Access-Control-Allow-Origin": origin, "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type", "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        })

    @app.post("/api/capture")
    def capture(payload: CaptureRequest, request: Request):
        auth = request.headers.get("authorization", "")
        bearer = auth[7:] if auth.startswith("Bearer ") else ""
        if payload.source == "extension" and not hmac.compare_digest(bearer, token):
            raise HTTPException(401, "Set the capture token from NDM's Media grabber page.")
        result = manager.capture(**payload.model_dump())
        response = JSONResponse(result)
        origin = request.headers.get("origin", "")
        if re.fullmatch(r"(?:chrome|moz)-extension://[a-p]{32}", origin):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        return response

    @app.get("/api/capture-extension")
    def capture_extension():
        import io
        import zipfile
        source = Path(__file__).parent / "extension"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in source.rglob("*"):
                if path.is_file():
                    archive.write(path, "ndm-capture/" + str(path.relative_to(source)))
        return Response(buffer.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="ndm-capture.zip"'})

    @app.get("/api/downloads/{task_id}/file")
    def download_file(task_id: str):
        task = manager.tasks.get(task_id)
        if not task:
            raise KeyError("Download not found.")
        path = Path(task["path"])
        if task["status"] != "completed" or not path.is_file():
            raise KeyError("The file is not ready yet.")
        return FileResponse(path, filename=task["name"], content_disposition_type="attachment")

    @app.get("/api/library")
    def library():
        return {"tracks": manager.library(), "folder": manager.settings.music_dir,
                "message": "Files on the machine running the daemon. The hosted preview only shows its sample workspace." if manager.hosted else ""}

    @app.get("/api/library/playlist.m3u")
    def playlist():
        tracks = manager.library()
        content = "#EXTM3U\n" + "\n".join(f"#EXTINF:-1,{Path(t['name']).stem}\n{t['path']}" for t in tracks)
        return Response(content, media_type="audio/x-mpegurl", headers={"Content-Disposition": 'attachment; filename="NDM-offline.m3u"'})

    @app.get("/api/library/{track_id}/stream")
    def stream(track_id: str):
        track = next((t for t in manager.library() if t["id"] == track_id), None)
        if not track:
            raise KeyError("Track not found. Refresh your library.")
        return FileResponse(track["path"], filename=track["name"])

    @app.post("/api/open/{task_id}")
    def open_local(task_id: str):
        if manager.hosted:
            raise ValueError("Open the downloaded file using Save file in this preview. Desktop mode can open local folders.")
        task = manager.tasks.get(task_id)
        if not task or task["status"] != "completed":
            raise KeyError("Completed download not found.")
        import subprocess
        import sys
        path = Path(task["path"])
        if sys.platform == "win32":
            import os
            os.startfile(str(path.parent))  # noqa: S606
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path.parent)])
        return {"ok": True}

    if demo_port is not None:
        from .demo import register_demo
        register_demo(app, demo_port)

    @app.get("/")
    def index():
        if (static_dir / "index.html").is_file():
            return FileResponse(static_dir / "index.html")
        return JSONResponse({"name": "NDM", "message": "Build the companion with npm install && npm run build, or use ndm desktop."})

    @app.get("/{path:path}")
    def static_files(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "Endpoint not found.")
        candidate = (static_dir / path).resolve()
        if candidate.is_relative_to(static_dir.resolve()) and candidate.is_file():
            return FileResponse(candidate)
        if not Path(path).suffix and (static_dir / "index.html").is_file():
            return FileResponse(static_dir / "index.html")
        raise HTTPException(404, "File not found.")

    return app


def run_server(*, data_dir: Path | None = None, host: str = "127.0.0.1", port: int = 8765,
               demo: bool = False, allow_private: bool = False):
    import uvicorn

    from .models import default_data_dir
    data_dir = data_dir or default_data_dir()
    store = Store(data_dir)
    if demo and not store.get_meta("settings"):
        from .models import Settings
        store.set_meta("settings", Settings(download_dir=str(data_dir / "downloads"),
                                            music_dir=str(data_dir / "music")).model_dump())
    try:
        manager = DownloadManager(store, hosted=host not in {"127.0.0.1", "::1", "localhost"},
                                  allow_private=allow_private or demo)
    except Exception:
        store.close()
        raise
    if demo:
        from .demo import seed_demo
        manager.allow_private = False
        manager.demo_port = port
        seed_demo(manager, port)
    app = create_app(manager, demo_port=port if demo else None)
    try:
        uvicorn.run(app, host=host, port=port, log_level="info", proxy_headers=True,
                    forwarded_allow_ips="*" if host == "0.0.0.0" else "127.0.0.1")
    finally:
        manager.shutdown()
