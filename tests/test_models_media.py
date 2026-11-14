from __future__ import annotations

import http.server
import threading

import pytest
from pydantic import ValidationError

from ndm.media import inspect_media, quality_format, spotify_link, uses_ytdlp
from ndm.models import Settings, SettingsPatch, safe_filename, validate_url


def test_filename_cannot_escape_destination():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("..\\..\\secret.txt") == "secret.txt"
    assert "\x00" not in safe_filename("bad\x00name.txt")
    assert safe_filename("") == "download"


def test_url_rejects_credentials_and_non_http():
    for url in ["file:///etc/passwd", "https://user:pass@example.com/a", "javascript:alert(1)"]:
        with pytest.raises(ValueError):
            validate_url(url)
    with pytest.raises(ValueError):
        validate_url("http://127.0.0.1/private.zip")
    assert validate_url("http://127.0.0.1/private.zip", allow_private=True).startswith("http://")


def test_hosted_demo_only_allows_its_own_fixture():
    import tempfile
    from pathlib import Path

    from ndm.engine import DownloadManager
    from ndm.store import Store
    with tempfile.TemporaryDirectory() as directory:
        manager = DownloadManager(Store(Path(directory) / "state"), hosted=True)
        try:
            manager.demo_port = 3000
            assert manager.validate_target("http://127.0.0.1:3000/demo/file.zip")
            with pytest.raises(ValueError):
                manager.validate_target("http://127.0.0.1:3000/api/state")
            with pytest.raises(ValueError):
                manager.validate_target("http://169.254.169.254/latest/meta-data")
        finally:
            manager.shutdown()


def test_settings_validate_colors_and_limits():
    with pytest.raises(ValidationError):
        Settings(accent="not-a-color")
    with pytest.raises(ValidationError):
        SettingsPatch(connections=99)
    with pytest.raises(ValidationError):
        Settings(theme="neon")
    assert Settings(accent="#AABBCC").accent == "#aabbcc"


def test_spotify_is_recognized_not_extracted():
    url = "https://open.spotify.com/track/example"
    assert spotify_link(url)
    result = inspect_media(url, allow_private=True)
    assert result["kind"] == "spotify"
    assert result["items"] == []
    assert "Premium offline" in result["message"]


def test_manifest_uses_extractor():
    assert uses_ytdlp("https://example.com/live/master.m3u8")
    assert uses_ytdlp("https://youtu.be/example")
    assert not uses_ytdlp("https://example.com/song.mp3")
    assert "acodec" in quality_format("1080", False)
    assert "+bestaudio" in quality_format("720", True)


def test_html_media_discovery():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = b'<html><title>My page</title><video src="/clip.mp4"></video><audio src="audio/song.mp3"></audio><a href="/not-media.txt">text</a></html>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = inspect_media(f"http://127.0.0.1:{server.server_port}/page.html", allow_private=True)
        assert result["kind"] == "html"
        assert result["title"] == "My page"
        assert {item["category"] for item in result["items"]} == {"video", "audio"}
        assert all(item["url"].startswith("http://127.0.0.1:") for item in result["items"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
