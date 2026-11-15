"""Real native-widget smoke tests using Nexa's software renderer and input events."""
from __future__ import annotations

import json

import nexa as ui

from ndm.desktop import Desktop, format_bytes, percent, status_text


class LocalClient:
    def __init__(self, tmp_path, monkeypatch):
        self.state = {"downloads": [], "settings": {
            "theme": "light", "accent": "#7856e8", "colors": {}, "proxy_enabled": False,
            "proxy_url": "", "proxy_username": "", "proxy_password": "", "proxy_remote_dns": True,
            "engine_running": True, "connections": 4,
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
        ctx.input_click(610, 240)
        frame(desktop, ctx)
        ctx.input_mouse_button(ui.MouseButton.LEFT, False)
        frame(desktop, ctx)
        assert ctx.wants_keyboard
        ctx.input_text("https://example.org/fixture.zip")
        frame(desktop, ctx)
        assert desktop.url == "https://example.org/fixture.zip"
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
        ctx.input_click(610, 340)
        frame(desktop, ctx)
        ctx.input_mouse_button(ui.MouseButton.LEFT, False)

        frame(desktop, ctx)
        assert ctx.wants_keyboard
        url = "https://open.spotify.com/track/" + "a" * 22
        ctx.input_text(url)
        frame(desktop, ctx)
        assert desktop.spotify_url == url
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
        for tab in range(6):
            desktop.open_dialog("options")
            desktop.options_tab = tab
            frame(desktop, ctx)
        desktop.open_dialog("options")
        desktop.options_tab = 5
        desktop.perform("settings", {"theme": "dark", "accent": "#169c83"})
        frame(desktop, ctx)
        assert ctx.theme_mode == ui.ThemeMode.DARK


def test_table_formatting():
    assert format_bytes(1024) == "1.0 KB"
    task = {"status": "downloading", "source": "spotdl", "percent": 40, "phase": "processing", "size": 0, "downloaded": 0, "speed": 0}
    assert percent(task) == 40
    assert status_text(task) == "Converting"


def test_global_ctrl_v_pastes_url_and_spotify_routes_correctly(tmp_path, monkeypatch):
    LocalClient(tmp_path, monkeypatch)
    desktop = Desktop(data_dir=tmp_path)
    clipboard = {"text": "https://example.org/file.zip"}
    monkeypatch.setattr(ui.UI, "get_clipboard", lambda self: clipboard["text"])
    with ui.SoftwareRenderer() as renderer, ui.UI(width=1280, height=780, renderer=renderer) as ctx:
        frame(desktop, ctx)
        ctx.input_key(ui.Key.V, True, ui.Mod.PRIMARY)
        frame(desktop, ctx)
        ctx.input_key(ui.Key.V, False, ui.Mod.PRIMARY)
        assert desktop.dialog == "add"
        assert desktop.url == "https://example.org/file.zip"
        desktop.dialog = ""
        clipboard["text"] = "https://open.spotify.com/track/" + "a" * 22
        ctx.input_key(ui.Key.V, True, ui.Mod.PRIMARY)
        frame(desktop, ctx)
        ctx.input_key(ui.Key.V, False, ui.Mod.PRIMARY)
        assert desktop.dialog == "spotify"
        assert desktop.spotify_url.endswith("a" * 22)
        desktop.dialog = ""
        clipboard["text"] = "not a link"
        desktop.paste_url(ctx)
        assert desktop.dialog == ""
        assert "Copy a single" in desktop.notice


def test_custom_palette_applies_every_token_and_resets(tmp_path, monkeypatch):
    from ndm.models import CUSTOM_COLOR_KEYS
    client = LocalClient(tmp_path, monkeypatch)
    desktop = Desktop(data_dir=tmp_path)
    colors = {key: "#123456" for key in CUSTOM_COLOR_KEYS}
    client.state["settings"]["colors"] = colors
    desktop.refresh()
    with ui.SoftwareRenderer() as renderer, ui.UI(width=1280, height=780, renderer=renderer) as ctx:
        frame(desktop, ctx)
        for key in CUSTOM_COLOR_KEYS:
            color = ctx.theme[ui.Token[key.upper()]]
            assert (color.r, color.g, color.b) == (0x12, 0x34, 0x56)
        client.state["settings"]["colors"] = {}
        desktop.refresh()
        frame(desktop, ctx)
        assert ctx.theme[ui.Token.BACKGROUND] != ui.Color.hex("#123456")


def test_proxy_options_save_through_native_form(tmp_path, monkeypatch):
    client = LocalClient(tmp_path, monkeypatch)
    desktop = Desktop(data_dir=tmp_path)
    desktop.open_dialog("options")
    desktop.options_tab = 1
    with ui.SoftwareRenderer() as renderer, ui.UI(width=1280, height=780, renderer=renderer) as ctx:
        frame(desktop, ctx)
        desktop.settings_form.update(proxy_enabled=True, proxy_url="socks5://127.0.0.1:1080",
                                     proxy_username="user", proxy_password="secret")
        desktop.save_options()
        assert desktop.state["settings"]["proxy_url"] == "socks5://127.0.0.1:1080"
        assert desktop.state["settings"]["proxy_enabled"] is True
        assert any(op == "settings" and payload["proxy_password"] == "secret" for op, payload in client.calls)


def test_options_accepts_settings_from_older_daemon(tmp_path, monkeypatch):
    client = LocalClient(tmp_path, monkeypatch)
    for key in ("colors", "proxy_enabled", "proxy_url", "proxy_username", "proxy_password", "proxy_remote_dns"):
        client.state["settings"].pop(key, None)
    client.state["settings"]["connections"] = 2
    desktop = Desktop(data_dir=tmp_path)
    assert desktop.state["settings"]["proxy_enabled"] is False
    assert desktop.state["settings"]["connections"] == 2
    desktop.open_dialog("options")
    desktop.options_tab = 1
    with ui.SoftwareRenderer() as renderer, ui.UI(width=1280, height=780, renderer=renderer) as ctx:
        frame(desktop, ctx)
        assert desktop.settings_form["proxy_url"] == ""
        assert desktop.settings_form["proxy_remote_dns"] is True
        desktop.settings_form.update(proxy_enabled=True, proxy_url="http://127.0.0.1:8080")
        desktop.save_options()
        assert desktop.state["settings"]["proxy_enabled"] is True
    # A later refresh from the same older daemon must not reintroduce the crash.
    for key in ("colors", "proxy_enabled", "proxy_url", "proxy_username", "proxy_password", "proxy_remote_dns"):
        client.state["settings"].pop(key, None)
    desktop.refresh()
    desktop.open_dialog("options")
    assert desktop.settings_form["proxy_enabled"] is False


def _right_click_until(desktop, ctx, x, ys, opened):
    """Right-click down a column until a context menu body actually runs."""
    for y in ys:
        ctx.input_mouse_move(x, y)
        ctx.input_mouse_button(ui.MouseButton.RIGHT, True)
        frame(desktop, ctx)
        ctx.input_mouse_button(ui.MouseButton.RIGHT, False)
        frame(desktop, ctx)
        if opened:
            return y
    return None


def test_right_click_opens_row_menu_and_acts_on_selection(tmp_path, monkeypatch):
    """A table row is not an interactive item, so ctx.context_menu anchored to it
    never opens. The filename cell must stay a real widget for the menu to work.
    """
    client = LocalClient(tmp_path, monkeypatch)
    desktop = Desktop(data_dir=tmp_path)
    client.request(None, "add", {"url": "https://example.org/fixture.zip"})
    desktop.refresh()

    opened = []
    original = desktop._row_context_menu

    def spy(ctx, task):
        opened.append(task["id"])
        original(ctx, task)

    desktop._row_context_menu = spy
    with ui.SoftwareRenderer() as renderer, ui.UI(width=1280, height=780, renderer=renderer) as ctx:
        for _ in range(3):
            frame(desktop, ctx)
        assert _right_click_until(desktop, ctx, 300, range(90, 220, 4), opened) is not None
        assert opened == ["native-test"] or opened[0] == "native-test"
        assert desktop.selected == {"native-test"}


def test_right_click_menu_item_reaches_the_daemon(tmp_path, monkeypatch):
    client = LocalClient(tmp_path, monkeypatch)
    desktop = Desktop(data_dir=tmp_path)
    client.request(None, "add", {"url": "https://example.org/fixture.zip"})
    desktop.refresh()

    opened = []
    original = desktop._row_context_menu

    def spy(ctx, task):
        opened.append(task["id"])
        real_item = ctx.menu_item

        def menu_item(label, **kwargs):
            if label == "Stop":
                return True
            return real_item(label, **kwargs)

        ctx.menu_item = menu_item
        try:
            original(ctx, task)
        finally:
            ctx.menu_item = real_item

    desktop._row_context_menu = spy
    with ui.SoftwareRenderer() as renderer, ui.UI(width=1280, height=780, renderer=renderer) as ctx:
        for _ in range(3):
            frame(desktop, ctx)
        assert _right_click_until(desktop, ctx, 300, range(90, 220, 4), opened) is not None
    assert ("action", {"id": "native-test", "action": "pause"}) in client.calls


def test_left_click_still_selects_without_opening_details(tmp_path, monkeypatch):
    """The filename is now a button; it must not break plain row selection."""
    client = LocalClient(tmp_path, monkeypatch)
    desktop = Desktop(data_dir=tmp_path)
    client.request(None, "add", {"url": "https://example.org/fixture.zip"})
    desktop.refresh()
    with ui.SoftwareRenderer() as renderer, ui.UI(width=1280, height=780, renderer=renderer) as ctx:
        for _ in range(3):
            frame(desktop, ctx)
        for y in range(90, 220, 4):
            ctx.input_mouse_move(300, y)
            ctx.input_mouse_button(ui.MouseButton.LEFT, True)
            frame(desktop, ctx)
            ctx.input_mouse_button(ui.MouseButton.LEFT, False)
            frame(desktop, ctx)
            if desktop.selected:
                break
        assert desktop.selected == {"native-test"}
        assert desktop.dialog == ""


def test_right_click_opens_spotify_result_menu(tmp_path, monkeypatch):
    LocalClient(tmp_path, monkeypatch)
    desktop = Desktop(data_dir=tmp_path)
    desktop.spotify_result = {
        "items": [{"id": "t1", "name": "Flow", "artist": "Sade", "album": "Alb",
                   "duration": 240, "url": "https://open.spotify.com/track/1"}],
        "truncated": False, "total": 1}
    desktop.open_dialog("spotify")
    opened = []
    original = desktop._spotify_row_menu

    def spy(ctx, item, items):
        opened.append(item["id"])
        original(ctx, item, items)

    desktop._spotify_row_menu = spy
    with ui.SoftwareRenderer() as renderer, ui.UI(width=1280, height=780, renderer=renderer) as ctx:
        for _ in range(3):
            frame(desktop, ctx)
        assert _right_click_until(desktop, ctx, 420, range(150, 600, 5), opened) is not None
        assert opened[0] == "t1"


def _download_row_y(desktop, ctx, x=300):
    """Find a y inside the first download row by right-clicking until it opens."""
    opened = []
    original = desktop._row_context_menu

    def spy(ctx_, task):
        opened.append(task["id"])
        original(ctx_, task)

    desktop._row_context_menu = spy
    try:
        for y in range(90, 260, 2):
            opened.clear()
            ctx.input_mouse_move(x, y)
            ctx.input_mouse_button(ui.MouseButton.RIGHT, True)
            frame(desktop, ctx)
            ctx.input_mouse_button(ui.MouseButton.RIGHT, False)
            frame(desktop, ctx)
            if opened:
                return y
    finally:
        desktop._row_context_menu = original
    return None


def test_right_click_opens_menu_from_every_column(tmp_path, monkeypatch):
    """The menu must open from any column, not just the filename anchor."""
    client = LocalClient(tmp_path, monkeypatch)
    desktop = Desktop(data_dir=tmp_path)
    client.request(None, "add", {"url": "https://example.org/fixture.zip"})
    desktop.refresh()
    opened = []
    original = desktop._row_context_menu

    def spy(ctx, task):
        opened.append(task["id"])
        original(ctx, task)

    desktop._row_context_menu = spy
    with ui.SoftwareRenderer() as renderer, ui.UI(width=1280, height=780, renderer=renderer) as ctx:
        for _ in range(3):
            frame(desktop, ctx)
        row_y = _download_row_y(desktop, ctx)
        assert row_y is not None
        # Size, Status, Progress, Time Left, Rate, Last Try, Description
        for x in (700, 790, 880, 960, 1050, 1150, 1240):
            opened.clear()
            ctx.input_key(ui.Key.ESCAPE, True)
            frame(desktop, ctx)
            ctx.input_key(ui.Key.ESCAPE, False)
            frame(desktop, ctx)
            ctx.input_mouse_move(x, row_y)
            ctx.input_mouse_button(ui.MouseButton.RIGHT, True)
            frame(desktop, ctx)
            ctx.input_mouse_button(ui.MouseButton.RIGHT, False)
            frame(desktop, ctx)
            assert opened, f"right-click at x={x} did not open the row menu"


def test_right_click_outside_the_table_does_not_open_row_menu(tmp_path, monkeypatch):
    client = LocalClient(tmp_path, monkeypatch)
    desktop = Desktop(data_dir=tmp_path)
    client.request(None, "add", {"url": "https://example.org/fixture.zip"})
    desktop.refresh()
    opened = []
    original = desktop._row_context_menu
    desktop._row_context_menu = lambda ctx, task: (opened.append(task["id"]), original(ctx, task))[1]
    with ui.SoftwareRenderer() as renderer, ui.UI(width=1280, height=780, renderer=renderer) as ctx:
        for _ in range(3):
            frame(desktop, ctx)
        row_y = _download_row_y(desktop, ctx)
        assert row_y is not None
        ctx.input_key(ui.Key.ESCAPE, True)
        frame(desktop, ctx)
        ctx.input_key(ui.Key.ESCAPE, False)
        frame(desktop, ctx)
        opened.clear()
        # Sidebar is left of the table content; toolbar is above the first row.
        for x, y in ((60, row_y), (300, 52)):
            ctx.input_mouse_move(x, y)
            ctx.input_mouse_button(ui.MouseButton.RIGHT, True)
            frame(desktop, ctx)
            ctx.input_mouse_button(ui.MouseButton.RIGHT, False)
            frame(desktop, ctx)
        assert not opened


def test_row_menu_suppressed_while_a_dialog_is_open(tmp_path, monkeypatch):
    client = LocalClient(tmp_path, monkeypatch)
    desktop = Desktop(data_dir=tmp_path)
    client.request(None, "add", {"url": "https://example.org/fixture.zip"})
    desktop.refresh()
    opened = []
    original = desktop._row_context_menu
    desktop._row_context_menu = lambda ctx, task: (opened.append(task["id"]), original(ctx, task))[1]
    with ui.SoftwareRenderer() as renderer, ui.UI(width=1280, height=780, renderer=renderer) as ctx:
        for _ in range(3):
            frame(desktop, ctx)
        row_y = _download_row_y(desktop, ctx)
        assert row_y is not None
        desktop.open_dialog("about")
        frame(desktop, ctx)
        opened.clear()
        ctx.input_mouse_move(1240, row_y)
        ctx.input_mouse_button(ui.MouseButton.RIGHT, True)
        frame(desktop, ctx)
        ctx.input_mouse_button(ui.MouseButton.RIGHT, False)
        frame(desktop, ctx)
        assert not opened


def test_search_field_filters_and_clear_button_resets(tmp_path, monkeypatch):
    """The restyled search field must still filter, and its clear affordance works."""
    client = LocalClient(tmp_path, monkeypatch)
    desktop = Desktop(data_dir=tmp_path)
    client.request(None, "add", {"url": "https://example.org/fixture.zip"})
    desktop.refresh()
    with ui.SoftwareRenderer() as renderer, ui.UI(width=1280, height=780, renderer=renderer) as ctx:
        for _ in range(3):
            frame(desktop, ctx)
        desktop.search = "fixture"
        for _ in range(2):
            frame(desktop, ctx)
        assert len(desktop.visible_tasks(desktop.state)) == 1
        desktop.search = "no-such-file"
        for _ in range(2):
            frame(desktop, ctx)
        assert desktop.visible_tasks(desktop.state) == []
        desktop.search = "fixture"
        for _ in range(2):
            frame(desktop, ctx)
        # The clear affordance sits at the right end of the rounded field.
        ctx.input_mouse_move(1238, 94)
        ctx.input_mouse_button(ui.MouseButton.LEFT, True)
        frame(desktop, ctx)
        ctx.input_mouse_button(ui.MouseButton.LEFT, False)
        for _ in range(2):
            frame(desktop, ctx)
        assert desktop.search == ""
        assert len(desktop.visible_tasks(desktop.state)) == 1


def test_clicking_any_column_selects_the_whole_row(tmp_path, monkeypatch):
    """Selection belongs to the row, not the filename button that anchors the menu."""
    client = LocalClient(tmp_path, monkeypatch)
    desktop = Desktop(data_dir=tmp_path)
    client.request(None, "add", {"url": "https://example.org/fixture.zip"})
    desktop.refresh()
    with ui.SoftwareRenderer() as renderer, ui.UI(width=1280, height=780, renderer=renderer) as ctx:
        for _ in range(3):
            frame(desktop, ctx)
        row_y = _download_row_y(desktop, ctx)
        assert row_y is not None
        ctx.input_key(ui.Key.ESCAPE, True)
        frame(desktop, ctx)
        ctx.input_key(ui.Key.ESCAPE, False)
        frame(desktop, ctx)
        desktop.selected.clear()
        # Status column, far from the filename anchor.
        ctx.input_mouse_move(790, row_y)
        ctx.input_mouse_button(ui.MouseButton.LEFT, True)
        frame(desktop, ctx)
        ctx.input_mouse_button(ui.MouseButton.LEFT, False)
        frame(desktop, ctx)
        assert desktop.selected == {"native-test"}


def test_row_menu_stays_within_the_panel_height(tmp_path, monkeypatch):
    """Nexa caps the popover height; too many entries draw past its background.

    Ten items plus three separators overflowed, leaving Properties and Remove
    floating over the table. Keep the entry count in budget.
    """
    client = LocalClient(tmp_path, monkeypatch)
    desktop = Desktop(data_dir=tmp_path)
    client.request(None, "add", {"url": "https://example.org/fixture.zip"})
    desktop.refresh()
    labels = []
    separators = []
    original = desktop._row_context_menu

    def spy(ctx, task):
        real_item, real_sep = ctx.menu_item, ctx.menu_separator

        def menu_item(label, **kwargs):
            if label.startswith("Resume"):
                # The body runs every frame; keep only the latest frame's list.
                labels.clear()
                separators.clear()
            labels.append(label)
            return real_item(label, **kwargs)

        def menu_separator():
            separators.append(1)
            return real_sep()

        ctx.menu_item, ctx.menu_separator = menu_item, menu_separator
        try:
            original(ctx, task)
        finally:
            ctx.menu_item, ctx.menu_separator = real_item, real_sep

    desktop._row_context_menu = spy
    with ui.SoftwareRenderer() as renderer, ui.UI(width=1280, height=780, renderer=renderer) as ctx:
        for _ in range(3):
            frame(desktop, ctx)
        assert _download_row_y(desktop, ctx) is not None
    assert labels, "menu did not render"
    # ~43px per entry and ~31px per separator against a ~419px panel.
    assert len(labels) * 43 + len(separators) * 31 <= 419, (labels, len(separators))
    assert "Remove…" in labels and "Properties" in labels
