from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from ndm.server import create_app

HEADERS = {"X-NDM-Client": "1"}


@pytest.fixture
def client(manager):
    return TestClient(create_app(manager))


def test_state_and_settings(client):
    response = client.get("/api/state")
    assert response.status_code == 200
    assert response.json()["stats"]["total"] == 0
    response = client.patch("/api/settings", headers=HEADERS, json={"accent": "#169c83", "theme": "dark", "connections": 2})
    assert response.status_code == 200
    state = client.get("/api/state").json()
    assert state["settings"]["accent"] == "#169c83"
    assert state["settings"]["theme"] == "dark"
    assert state["settings"]["connections"] == 2


def test_csrf_header_and_origin_required(client):
    assert client.post("/api/downloads", json={"url": "https://example.com/a.zip"}).status_code == 403
    assert client.patch("/api/settings", json={"theme": "dark"}).status_code == 403
    response = client.patch("/api/settings", headers={**HEADERS, "Origin": "https://evil.example"}, json={"theme": "dark"})
    assert response.status_code == 403


def test_capture_token_is_scoped(client):
    config = client.get("/api/capture-config").json()
    token = config["token"]
    assert len(token) > 20
    response = client.patch("/api/settings", headers={"Authorization": f"Bearer {token}"}, json={"theme": "dark"})
    assert response.status_code == 403
    response = client.post("/api/capture", headers={"Authorization": f"Bearer {token}"}, json={
        "url": "https://example.com/a.mp4", "source": "extension", "authorized": True,
    })
    assert response.status_code == 409  # capture is opt-in
    assert client.post("/api/capture", json={"url": "https://example.com/a.mp4", "source": "extension", "authorized": True}).status_code == 403


def test_extension_preflight_only_for_extensions(client):
    response = client.options("/api/capture", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert response.status_code == 403
    origin = "chrome-extension://" + "a" * 32
    response = client.options("/api/capture", headers={"Origin": origin, "Access-Control-Request-Method": "POST"})
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == origin


def test_extension_download_is_valid_zip(client):
    response = client.get("/api/capture-extension")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert "ndm-capture/manifest.json" in archive.namelist()
        assert "ndm-capture/background.js" in archive.namelist()


def test_spotify_add_is_redirected_not_ripped(client):
    response = client.post("/api/downloads", headers=HEADERS, json={"url": "https://open.spotify.com/track/example", "authorized": True})
    assert response.status_code == 400
    assert "Spotify" in response.json()["detail"]


def test_download_permission_required(client):
    response = client.post("/api/downloads", headers=HEADERS, json={"url": "https://example.com/song.mp3"})
    assert response.status_code == 400
    assert "permission" in response.json()["detail"]


def test_invalid_settings_do_not_change_state(client):
    response = client.patch("/api/settings", headers=HEADERS, json={"connections": 99})
    assert response.status_code == 422
    assert client.get("/api/state").json()["settings"]["connections"] == 4


def test_library_playlist_empty(client):
    response = client.get("/api/library/playlist.m3u")
    assert response.status_code == 200
    assert response.text.startswith("#EXTM3U")
    assert "attachment" in response.headers["content-disposition"]
