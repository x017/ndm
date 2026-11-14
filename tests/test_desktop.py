"""Real native-widget smoke tests using Nexa's software renderer and input events."""
from __future__ import annotations

import json

import nexa as ui

from ndm.desktop import Desktop, format_bytes, percent, status_text


class LocalClient:
    def __init__(self, tmp_path, monkeypatch):
        self.state = {"downloads": [], "settings": {
            "theme": "light", "accent": "#7856e8", "engine_running": True, "connections": 4,
            "max_concurrent": 3, "speed_limit": 0, "resume_on_start": True, "notifications": False,
            "download_dir": str(tmp_path / "downloads"), "music_dir": str(tmp_path / "music"),
            "auto_capture": False, "clipboard_capture": False, "watch_inbox": False,
            "allowed_domains": [], "spotify_format": "mp3", "spotify_bitrate": "192k"},
            "stats": {"active": 0, "queued": 0, "speed": 0, "total": 0},
            "daemon": {"inbox": str(tmp_path / "inbox"), "spotify": {"installed": True, "version": "4.6.0", "ffmpeg": True}}}
        self.calls = []
        monkeypatch.setattr("ndm.desktop.is_running", lambda path: True)
        monkeypatch.setattr("ndm.desktop.request", self.request)

    def request(self, path, operation, payload=None, **kwargs):
        self.calls.append((operation, payload))
        if operation == "state":
            return json.loads(json.dumps(self.state))
        if operation == "settings":
            self.state["settings"].update(payload)
            return self.state["settings"]
        if operation == "add":
            task = {"id": "native-test", "name": payload.get("filename") or "fixture.zip", "size": 1024,
                    "downloaded": 256, "speed": 100, "status": "downloading", "category": "archive",
                    "created_at": 1760000000, "host": "example.org", "url": payload["url"], "source": "manual",
                    "segments": [], "connections": 4, "path": "/tmp/fixture.zip", "resumable": True, "error": ""}
            self.state["downloads"] = [task]
            return task
        if operation == "action":
            if payload["action"] == "delete":
                self.state["downloads"] = []
            return {"ok": True}
        return {"ok": True, "items": [], "tracks": [], "tasks": payload.get("songs", []) if payload else []}


def click(ctx, target):
    if isinstance(target, tuple):
        x, y = target
    else:
        rect = ctx.get_rect(target)
        assert rect.w > 0 and rect.h > 0
        x, y = rect.x + rect.w / 2, rect.y + rect.h / 2
    ctx.input_click(x, y)


def frame(desktop, ctx):
    with ctx.frame(dt=0.1):
        desktop.draw(ctx)


def test_real_toolbar_dialog_form_and_category_tree(tmp_path, monkeypatch):
    client = LocalClient(tmp_path, monkeypatch)
    desktop = Desktop(data_dir=tmp_path)
    buttons = {}
    original_toolbar = desktop._toolbar

    def toolbar(ctx):
        original_toolbar(ctx)
        # The toolbar creates buttons with their visible labels; record the
        # resolved layout rectangles after the frame finishes.
        buttons.update({"Add URL": (52, 52), "Spotify": (143, 52), "Options": (1230, 52)})

    desktop._toolbar = toolbar
    with ui.SoftwareRenderer() as renderer, ui.UI(width=1280, height=780, renderer=renderer) as ctx:
        frame(desktop, ctx)
        click(ctx, buttons["Add URL"])
        ctx.input_mouse_button(ui.MouseButton.LEFT, False)
        frame(desktop, ctx)
        assert desktop.dialog == "add"
        ctx.input_click(610, 230)
        frame(desktop, ctx)
        ctx.input_mouse_button(ui.MouseButton.LEFT, False)
        frame(desktop, ctx)
        assert ctx.wants_keyboard
        ctx.input_text("https://example.org/fixture.zip")
        frame(desktop, ctx)
        assert desktop.url == "https://example.org/fixture.zip"
        desktop.authorized = True
        desktop.add()
        assert any(operation == "add" for operation, _ in client.calls)
        frame(desktop, ctx)
        assert len(desktop.visible_tasks(desktop.state)) == 1
        desktop.category = "document"
        assert not desktop.visible_tasks(desktop.state)
        desktop.category = "unfinished"
        assert len(desktop.visible_tasks(desktop.state)) == 1
        desktop.category = "all"
        desktop.search = "not-a-match"
        assert not desktop.visible_tasks(desktop.state)
        desktop.search = ""
        frame(desktop, ctx)
        click(ctx, buttons["Options"])
        ctx.input_mouse_button(ui.MouseButton.LEFT, False)
        frame(desktop, ctx)
        assert desktop.dialog == "options"
        desktop.settings_form["connections"] = 2
        desktop.save_options()
        assert desktop.state["settings"]["connections"] == 2


def test_spotify_dialog_uses_native_inputs(tmp_path, monkeypatch):
    LocalClient(tmp_path, monkeypatch)
    desktop = Desktop(data_dir=tmp_path)
    desktop.open_dialog("spotify")
    with ui.SoftwareRenderer() as renderer, ui.UI(width=1280, height=780, renderer=renderer) as ctx:
        frame(desktop, ctx)
        ctx.input_click(610, 333)
        frame(desktop, ctx)
        ctx.input_mouse_button(ui.MouseButton.LEFT, False)

        frame(desktop, ctx)
        assert ctx.wants_keyboard
        url = "https://open.spotify.com/track/" + "a" * 22
        ctx.input_text(url)
        frame(desktop, ctx)
        assert desktop.spotify_url == url
        assert not desktop.spotify_authorized
        desktop.spotify_authorized = True
        desktop.spotify_result = {"items": [{"id": "a", "name": "Local song", "artist": "Local artist",
                                             "album": "Local album", "duration": 123, "song": {}}], "truncated": False}
        desktop.spotify_selected = {"a"}
        frame(desktop, ctx)
        desktop.queue_spotify()
        assert desktop.dialog == ""
        assert desktop.notice == "Queued 1 tracks through spotDL."


def test_all_native_dialogs_render(tmp_path, monkeypatch):
    LocalClient(tmp_path, monkeypatch)
    desktop = Desktop(data_dir=tmp_path)
    with ui.SoftwareRenderer() as renderer, ui.UI(width=1280, height=780, renderer=renderer) as ctx:
        for name in ("add", "spotify", "media", "options", "library", "delete", "shutdown", "about"):
            desktop.open_dialog(name)
            frame(desktop, ctx)
            assert desktop.dialog == name
        for tab in range(5):
            desktop.open_dialog("options")
            desktop.options_tab = tab
            frame(desktop, ctx)
        desktop.open_dialog("options")
        desktop.options_tab = 4
        desktop.perform("settings", {"theme": "dark", "accent": "#169c83"})
        frame(desktop, ctx)
        assert ctx.theme_mode == ui.ThemeMode.DARK


def test_table_formatting():
    assert format_bytes(1024) == "1.0 KB"
    task = {"status": "downloading", "source": "spotdl", "percent": 40, "phase": "processing", "size": 0, "downloaded": 0, "speed": 0}
    assert percent(task) == 40
    assert status_text(task) == "Converting"
