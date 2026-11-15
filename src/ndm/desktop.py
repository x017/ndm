"""Native IDM-style desktop application. Real Nexa widgets; no webview or web server."""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import nexa as ui

from .daemon import is_running, start_detached
from .ipc import request
from .models import (
    ACCENTS,
    CUSTOM_COLOR_KEYS,
    MEDIA_EXTENSIONS,
    SETTINGS_SCHEMA_VERSION,
    Settings,
    normalize_color,
)
from .network import proxy_label, safe_error

CATEGORY_NAMES = {"all": "All Downloads", "unfinished": "Unfinished", "finished": "Finished",
                  "archive": "Compressed", "document": "Documents", "audio": "Music",
                  "video": "Video", "image": "Images", "other": "Other"}
CATEGORY_ICONS = {"all": ui.Icon.DOWNLOAD, "unfinished": ui.Icon.CLOCK, "finished": ui.Icon.CIRCLE_CHECK,
                  "archive": ui.Icon.ARCHIVE, "document": ui.Icon.FILE_TEXT, "audio": ui.Icon.MUSIC,
                  "video": ui.Icon.VIDEO, "image": ui.Icon.IMAGE, "other": ui.Icon.FILE}


def format_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "0 B"


def format_size(size: float, *, estimated: bool = False) -> str:
    if not size:
        return "Unknown"
    return ("~" if estimated else "") + format_bytes(size)


def hex_color(color: ui.Color, *, keep_alpha: bool = True) -> str:
    value = f"#{color.r:02x}{color.g:02x}{color.b:02x}"
    if keep_alpha and color.a != 255:
        value += f"{color.a:02x}"
    return value


def mp3_size_estimate(item: dict) -> int:
    """192 kbps MP3 ≈ 24,000 bytes per second of audio."""
    try:
        duration = float(item.get("duration") or 0)
    except (TypeError, ValueError):
        return 0
    return int(duration * 24_000) if duration > 0 else 0


def media_size_label(item: dict, *, convert_mp3: bool = False) -> str:
    if convert_mp3:
        estimate = mp3_size_estimate(item)
        return "MP3 " + format_size(estimate, estimated=True) if estimate else "MP3 size unknown"
    return format_size(item.get("size") or 0, estimated=bool(item.get("size_estimated")))


def percent(task: dict) -> float:
    if task["status"] == "completed":
        return 100.0
    if task["source"] == "spotdl":
        return float(task.get("percent", 0))
    return min(100.0, task["downloaded"] / task["size"] * 100) if task["size"] else 0.0


def time_left(task: dict) -> str:
    if task["status"] == "completed":
        return ""
    if task["status"] != "downloading" or task["source"] in {"spotdl", "ytdlp"}:
        return "—"
    if not task["size"] or not task["speed"]:
        return "Estimating"
    seconds = max(0, int((task["size"] - task["downloaded"]) / task["speed"]))
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def status_text(task: dict) -> str:
    if task["status"] == "downloading":
        return {"processing": "Converting", "paused": "Stopping"}.get(task.get("phase"), "Downloading")
    return {"queued": "Queued", "paused": "Stopped", "completed": "Complete", "error": "Error"}.get(task["status"], task["status"])


def complete_settings(settings: dict | None) -> dict:
    """Fill settings added since a daemon/state file was created.

    The daemon validates persisted settings, but an older still-running daemon
    can return a partial dictionary over IPC. Normalize at the UI boundary so
    opening Options never depends on the daemon having been restarted first.
    """
    defaults = Settings().model_dump()
    if settings:
        defaults.update({key: value for key, value in settings.items() if key in Settings.model_fields})
    return Settings.model_validate(defaults).model_dump()


OPTIONS_KEYS = [
    "download_dir", "music_dir", "max_concurrent", "connections", "speed_limit", "resume_on_start",
    "notifications", "auto_capture", "clipboard_capture", "watch_inbox", "allowed_domains",
    "firefox_cookies", "spotify_format", "spotify_bitrate", "proxy_enabled", "proxy_url", "proxy_username",
    "proxy_password", "proxy_remote_dns",
]


class Desktop:
    def __init__(self, *, data_dir: Path):
        self.data_dir = data_dir.expanduser().resolve()
        self._ensure_daemon()
        self.state = self._normalized_state(self.call("state"))
        self.cache_lock = threading.RLock()
        self.stop = threading.Event()
        self.app = None
        self.runner = None
        self.category = "all"
        self.tree_open = True
        self.categories_open = True
        self.search = ""
        self.selected: set[str] = set()
        self.last_row = None
        self.last_click = 0.0
        self.sort = ui.TableSort(6, True)
        self.dialog = ""
        self.error = ""
        self.notice = ""
        self.delete_ids: list[str] = []
        self.details_id = None
        self.exit_after_stop = False
        self.url = ""
        self.filename = ""
        self.start_paused = False
        self.add_connections = self.state["settings"]["connections"]
        self.add_category = 0
        self.media_url = ""
        self.media_result = None
        self.media_convert_mp3 = False
        self.spotify_url = ""
        self.spotify_result = None
        self.spotify_selected: set[str] = set()
        self.spotify_started = 0.0
        self.busy = ""
        self.library = []
        self.library_selection = -1
        self.options_tab = 0
        self.settings_form = complete_settings(self.state["settings"])
        self.custom_color = ui.Color.hex(self.state["settings"]["accent"])
        self.color_hex: dict[str, str] = {}
        self.last_clipboard = ""
        self.last_clipboard_check = 0.0
        self.completed_ids = {t["id"] for t in self.state["downloads"] if t["status"] == "completed"}
        self._following_system = False
        self._theme_mode: int | None = None
        self._theme_pending = False
        self._base_palettes: dict[int, dict] = {}

    def call(self, operation: str, payload: dict | None = None, *, timeout: float = 30.0):
        return request(self.data_dir, operation, payload, timeout=timeout)

    def _ensure_daemon(self) -> None:
        if not is_running(self.data_dir):
            start_detached(self.data_dir)
            if not self._wait_for_daemon():
                raise RuntimeError(f"The native daemon did not start. See {self.data_dir / 'daemon.log'}.")
            return
        try:
            health = self.call("health", timeout=3)
        except (OSError, ValueError, RuntimeError, EOFError):
            return
        if health.get("name") != "ndm":
            return
        state = self.call("state", timeout=3)
        if not self._daemon_is_compatible(state):
            self._replace_daemon()

    @staticmethod
    def _daemon_is_compatible(state: dict) -> bool:
        schema = state.get("daemon", {}).get("settings_schema")
        if schema is not None and schema != SETTINGS_SCHEMA_VERSION:
            return False
        settings = state.get("settings") or {}
        required = set(OPTIONS_KEYS) | {"colors", "theme", "accent", "engine_running"}
        return required.issubset(settings)

    def _wait_for_daemon(self, *, compatible: bool = False, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if is_running(self.data_dir):
                if not compatible:
                    return True
                try:
                    state = self.call("state", timeout=2)
                except (OSError, ValueError, RuntimeError, EOFError):
                    state = None
                if state is not None and self._daemon_is_compatible(state):
                    return True
            time.sleep(0.1)
        return False

    def _daemon_lock_free(self) -> bool:
        path = self.data_dir / "daemon.lock"
        if not path.exists():
            return True
        try:
            with path.open("a+") as handle:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return True
        except OSError:
            return False

    def _replace_daemon(self) -> None:
        try:
            self.call("shutdown", timeout=5)
        except (OSError, ValueError, RuntimeError, EOFError):
            pass
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if not is_running(self.data_dir) and self._daemon_lock_free():
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("The old background daemon did not stop. Run 'ndm stop', then start NDM again.")
        start_detached(self.data_dir)
        if not self._wait_for_daemon(compatible=True):
            raise RuntimeError(f"The updated background daemon did not start. See {self.data_dir / 'daemon.log'}.")

    @staticmethod
    def _normalized_state(state: dict) -> dict:
        return {**state, "settings": complete_settings(state.get("settings"))}

    def refresh(self) -> None:
        try:
            state = self._normalized_state(self.call("state", timeout=3))
            with self.cache_lock:
                self.state = state
        except (OSError, ValueError, RuntimeError, EOFError) as exc:
            with self.cache_lock:
                self.error = f"Local daemon connection: {exc}"

    def _background(self) -> None:
        while not self.stop.wait(0.5):
            self.refresh()
            if self.app:
                self.app.wake()

    def open_dialog(self, name: str) -> None:
        self.dialog = name
        self.error = ""
        self.notice = ""
        if name == "options":
            self.settings_form = complete_settings(self.state["settings"])
            self.custom_color = ui.Color.hex(self.state["settings"]["accent"])
            self.color_hex = {}
        if name == "add":
            self.add_connections = self.state["settings"]["connections"]

    def perform(self, operation: str, payload: dict | None = None) -> None:
        try:
            self.call(operation, payload)
            self.error = ""
            self.refresh()
        except (OSError, ValueError, RuntimeError, EOFError) as exc:
            self.error = safe_error(exc, self.state.get("settings", {}))

    def targets(self) -> list[dict]:
        return [t for t in self.state["downloads"] if t["id"] in self.selected]

    def act_selected(self, action: str) -> None:
        for task in self.targets():
            self.perform("action", {"id": task["id"], "action": action})
        if action == "delete":
            self.selected.clear()

    def stop_all(self) -> None:
        for task in self.state["downloads"]:
            if task["status"] in {"downloading", "queued"}:
                self.perform("action", {"id": task["id"], "action": "pause"})

    def add(self) -> None:
        categories = [None, "video", "audio", "archive", "image", "document", "other"]
        try:
            self.call("add", {"url": self.url.strip(), "filename": self.filename.strip() or None,
                              "category": categories[self.add_category], "connections": self.add_connections,
                              "start_paused": self.start_paused})
            self.url = self.filename = ""
            self.start_paused = False
            self.dialog = ""
            self.category = "all"
            self.refresh()
        except (OSError, ValueError, RuntimeError, EOFError) as exc:
            self.error = safe_error(exc, self.state.get("settings", {}))

    def inspect_media(self) -> None:
        if self.busy:
            return
        url = self.media_url.strip()
        if not url:
            self.error = "Paste a page or media URL first."
            return
        self.media_url = url
        self.busy = "media"
        self.media_result = None
        self.error = ""

        def work():
            return self.call("media.inspect", {"url": url}, timeout=120)

        self.runner.submit(work, on_done=self._media_loaded, on_error=self._operation_error)

    def _media_loaded(self, result: dict) -> None:
        self.busy = ""
        self.media_result = result

    def _operation_error(self, exc: BaseException) -> None:
        self.busy = ""
        self.spotify_started = 0.0
        self.error = safe_error(exc, self.state.get("settings", {}))

    def search_spotify(self) -> None:
        if self.busy:
            return
        url = self.spotify_url.strip()
        if not url:
            self.error = "Paste a public Spotify track, album, or playlist link first."
            return
        self.spotify_url = url
        self.busy = "spotify"
        self.spotify_started = time.monotonic()
        self.error = ""
        self.spotify_result = None
        self.spotify_selected.clear()
        self.runner.submit(lambda: self.call("spotify.search", {"url": url}, timeout=300),
                           on_done=self._spotify_loaded, on_error=self._operation_error)

    def _spotify_loaded(self, result: dict) -> None:
        self.busy = ""
        self.spotify_started = 0.0
        self.spotify_result = result
        self.spotify_selected = {item["id"] for item in result["items"]}

    def queue_spotify(self, all_tracks: bool = False) -> None:
        if not self.spotify_result:
            return
        songs = [item["song"] for item in self.spotify_result["items"]
                 if all_tracks or item["id"] in self.spotify_selected]
        if not songs:
            self.error = "Select at least one track."
            return
        try:
            result = self.call("spotify.queue", {"songs": songs, "start_paused": self.start_paused})
            self.notice = f"Queued {len(result['tasks'])} tracks through spotDL."
            self.dialog = ""
            self.category = "audio"
            self.refresh()
        except (OSError, ValueError, RuntimeError, EOFError) as exc:
            self.error = safe_error(exc, self.state.get("settings", {}))

    def save_options(self) -> None:
        payload = {key: self.settings_form[key] for key in OPTIONS_KEYS}
        try:
            try:
                self.call("settings", payload)
            except ValueError as exc:
                text = str(exc)
                if "Extra inputs are not permitted" not in text and "extra_forbidden" not in text:
                    raise
                # An out-of-date daemon can survive an app update. Replace it and
                # retry once instead of asking the user to restart daemons by hand.
                self._replace_daemon()
                self.call("settings", payload)
            self.notice = "Options saved."
            self.dialog = ""
            self.refresh()
        except (OSError, ValueError, RuntimeError, EOFError) as exc:
            self.error = safe_error(exc, self.settings_form)

    def load_library(self) -> None:
        self.open_dialog("library")
        self.runner.submit(lambda: self.call("library")["tracks"],
                           on_done=lambda tracks: setattr(self, "library", tracks), on_error=self._operation_error)

    def apply_theme(self, ctx: ui.UI, settings: dict) -> None:
        # Nexa animates set_theme/follow_system_theme retargets and only applies
        # them at the next begin_frame. Keep theme motion at zero so the target
        # palette snaps in, and never capture a base palette mid-transition.
        ctx.theme.motion = 0
        mode = settings["theme"]
        if mode == "system":
            if not self._following_system and self.app is not None:
                previous = ctx.theme_mode
                try:
                    self.app.follow_system_theme(True)
                    self._following_system = True
                except RuntimeError:
                    ctx.theme_mode = ui.ThemeMode.LIGHT
                if ctx.theme_mode != previous:
                    self._theme_pending = True
        elif self._following_system:
            if self.app is not None:
                self.app.follow_system_theme(False)
            self._following_system = False
        if mode != "system":
            target = ui.ThemeMode.DARK if mode == "dark" else ui.ThemeMode.LIGHT
            if ctx.theme_mode != target:
                ctx.theme_mode = target
                self._theme_pending = True
        resolved = int(ctx.theme_mode)
        if self._theme_pending:
            # The retarget was requested during the previous frame (or just
            # now). This frame's colors are still the old palette; wait one
            # frame for Nexa to snap to the pristine target before capturing.
            self._theme_pending = False
            self._theme_mode = None
        else:
            if resolved != self._theme_mode or resolved not in self._base_palettes:
                # First frame, an OS theme switch, or the frame after a requested
                # mode change: ctx.theme is the untouched Nexa base palette.
                self._base_palettes[resolved] = {
                    token: ctx.theme[token] for token in ui.Token
                    if token not in {ui.Token.NONE, ui.Token.COUNT}
                }
                self._theme_mode = resolved
            # Restore the pristine base every frame so resetting one token, or
            # changing the accent, cannot leave stale custom values behind.
            for token, color in self._base_palettes[resolved].items():
                ctx.theme[token] = color
            accent = ui.Color.hex(settings["accent"])
            for token in (ui.Token.ACCENT, ui.Token.ACCENT_STRONG, ui.Token.CONTROL_ON, ui.Token.SERIES_1):
                ctx.theme[token] = accent
            ctx.theme[ui.Token.ACCENT_SUBTLE] = ui.with_alpha(accent, 0.15)
            ctx.theme[ui.Token.FOCUS_RING] = ui.with_alpha(accent, 0.5)
            luminance = accent.r * .299 + accent.g * .587 + accent.b * .114
            contrast = ui.Color.hex("#172033" if luminance > 165 else "#ffffff")
            ctx.theme[ui.Token.ACCENT_FOREGROUND] = contrast
            ctx.theme[ui.Token.CONTROL_GLYPH] = contrast
            ctx.theme[ui.Token.CONTROL_THUMB_ON] = contrast
            for key, value in settings.get("colors", {}).items():
                if key in CUSTOM_COLOR_KEYS:
                    ctx.theme[ui.Token[key.upper()]] = ui.Color.hex(value)
        # Dense, restrained desktop chrome rather than a dashboard theme.
        ctx.theme.radius_control = 3
        ctx.theme.radius_surface = 3
        ctx.theme.radius_panel = 4
        ctx.theme.text_body = 12

    def draw(self, ctx: ui.UI) -> None:
        with self.cache_lock:
            state = self.state
        settings = state["settings"]
        self.apply_theme(ctx, settings)
        if settings["notifications"]:
            for task in state["downloads"]:
                if task["status"] == "completed" and task["id"] not in self.completed_ids:
                    ctx.toast("Download complete", description=task["name"], tone=ui.Tone.SUCCESS)
                    self.completed_ids.add(task["id"])
        self._capture_clipboard(ctx, settings)
        with ctx.column(width=ui.FILL, height=ui.FILL, bg=ui.Color.BACKGROUND):
            self._menus(ctx, settings)
            self._toolbar(ctx)
            if self.error and not self.dialog:
                if ctx.alert(self.error[:350], tone=ui.Tone.WARNING, dismissible=True):
                    self.error = ""
            if self.notice and not self.dialog:
                if ctx.alert(self.notice, tone=ui.Tone.SUCCESS, dismissible=True):
                    self.notice = ""
            with ctx.row(width=ui.FILL, height=38, pad_x=10, gap=8, align=ui.Align.CENTER, bg=ui.Color.SURFACE_MUTED):
                ctx.icon(CATEGORY_ICONS[self.category], size=15)
                ctx.text(CATEGORY_NAMES[self.category], size=12, font=ui.FontRole.MEDIUM)
                ctx.spacer()
                ctx.text("Find:", size=11)
                self.search = ctx.input("find_downloads", self.search, placeholder="File name or address", width=245)
            with ctx.split("workspace", 0.18, min_first=175, min_second=620, width=ui.FILL, height=ui.FILL) as split:
                self._categories(ctx, state)
                split.next()
                self._download_table(ctx, state)
            with ctx.status_bar():
                ctx.status_bar_text(f"{len(self.state['downloads'])} files", icon=ui.Icon.FILE)
                ctx.status_bar_text(f"{state['stats']['active']} downloading", icon=ui.Icon.DOWNLOAD)
                ctx.status_bar_text(f"{state['stats']['queued']} queued", icon=ui.Icon.CLOCK)
                ctx.status_bar_spacer()
                ctx.status_bar_text("Proxy: " + proxy_label(settings), icon=ui.Icon.GLOBE)
                ctx.status_bar_text(f"Speed: {format_bytes(state['stats']['speed'])}/s", icon=ui.Icon.ACTIVITY)
                ctx.status_bar_text("Local daemon · no web server", icon=ui.Icon.SHIELD,
                                    tone=ui.Tone.SUCCESS if settings["engine_running"] else ui.Tone.WARNING)
        self._dialogs(ctx, state)
        # Handle global shortcuts only after every widget has run. Nexa clears
        # its keyboard-owner flag at begin_frame, so checking wants_keyboard
        # before the inputs are drawn is always false even when a field is
        # focused; that let the global Ctrl+V replace the model value and the
        # native field paste the same clipboard again (the doubled link).
        if not ctx.wants_keyboard:
            if ctx.shortcut(ui.Mod.PRIMARY, ui.Key.V) and self.dialog in {"", "add", "spotify", "media"}:
                self.paste_url(ctx)
            if ctx.shortcut(ui.Mod.PRIMARY, ui.Key.N):
                self.open_dialog("add")
            if ctx.shortcut(ui.Mod.PRIMARY, ui.Key.A):
                self.selected = {t["id"] for t in self.visible_tasks(state)}
            if ctx.key_pressed(ui.Key.DELETE) and self.selected:
                self.ask_delete(list(self.selected))
            if ctx.key_pressed(ui.Key.ENTER) and len(self.selected) == 1:
                self.details_id = next(iter(self.selected))
                self.open_dialog("details")

    def _menus(self, ctx: ui.UI, settings: dict) -> None:
        with ctx.menu_bar():
            with ctx.menu_bar_menu("File", mnemonic="F") as menu:
                if menu:
                    if ctx.menu_item("Add URL…", icon=ui.Icon.PLUS, shortcut="Ctrl+N"):
                        self.open_dialog("add")
                    if ctx.menu_item("Paste URL from clipboard", icon=ui.Icon.CLIPBOARD, shortcut="Ctrl+V"):
                        self.paste_url(ctx)
                    if ctx.menu_item("Download Spotify link…", icon=ui.Icon.MUSIC):
                        self.open_dialog("spotify")
                    ctx.menu_separator()
                    if ctx.menu_item("Close window (downloads continue)", icon=ui.Icon.LOG_OUT):
                        self.app.quit()
                    if ctx.menu_item("Stop daemon and exit", danger=True):
                        self.exit_after_stop = True
                        self.open_dialog("shutdown")
            with ctx.menu_bar_menu("Downloads", mnemonic="D") as menu:
                if menu:
                    if ctx.menu_item("Resume selected", icon=ui.Icon.PLAY, disabled=not self.selected):
                        self.act_selected("resume")
                    if ctx.menu_item("Stop selected", icon=ui.Icon.PAUSE, disabled=not self.selected):
                        self.act_selected("pause")
                    if ctx.menu_item("Stop all", icon=ui.Icon.SQUARE):
                        self.stop_all()
                    ctx.menu_separator()
                    if ctx.menu_item("Remove selected…", icon=ui.Icon.TRASH, disabled=not self.selected):
                        self.ask_delete(list(self.selected))
                    if ctx.menu_item("Properties", icon=ui.Icon.INFO, disabled=len(self.selected) != 1):
                        self.details_id = next(iter(self.selected))
                        self.open_dialog("details")
            with ctx.menu_bar_menu("View", mnemonic="V") as menu:
                if menu:
                    if ctx.menu_item("Refresh", icon=ui.Icon.REFRESH_CW, shortcut="F5"):
                        self.refresh()
                    if ctx.menu_item("Dark theme", checked=settings["theme"] == "dark"):
                        self.perform("settings", {"theme": "light" if settings["theme"] == "dark" else "dark"})
                    if ctx.menu_item("Select all", disabled=not state_has_tasks(self.state)):
                        self.selected = {t["id"] for t in self.visible_tasks(self.state)}
            with ctx.menu_bar_menu("Tools", mnemonic="T") as menu:
                if menu:
                    if ctx.menu_item("Media grabber…", icon=ui.Icon.LINK):
                        self.open_dialog("media")
                    if ctx.menu_item("Offline audio files…", icon=ui.Icon.MUSIC):
                        self.load_library()
                    if ctx.menu_item("Options…", icon=ui.Icon.SETTINGS):
                        self.open_dialog("options")
            with ctx.menu_bar_menu("Help", mnemonic="H") as menu:
                if menu:
                    if ctx.menu_item("About NDM", icon=ui.Icon.INFO):
                        self.open_dialog("about")
        if ctx.key_pressed(ui.Key.F5):
            self.refresh()

    def _toolbar(self, ctx: ui.UI) -> None:
        with ctx.toolbar():
            if ctx.toolbar_button(ui.Icon.PLUS, label="Add URL", tooltip="Add a direct download", shortcut="Ctrl+N"):
                self.open_dialog("add")
            if ctx.toolbar_button(ui.Icon.MUSIC, label="Spotify", tooltip="Find and queue a Spotify link with spotDL"):
                self.open_dialog("spotify")
            if ctx.toolbar_button(ui.Icon.LINK, label="Grabber", tooltip="Inspect a media page"):
                self.open_dialog("media")
            ctx.toolbar_separator()
            if ctx.toolbar_button(ui.Icon.PLAY, label="Resume", disabled=not self.selected, tone=ui.Tone.SUCCESS):
                self.act_selected("resume")
            if ctx.toolbar_button(ui.Icon.PAUSE, label="Stop", disabled=not self.selected):
                self.act_selected("pause")
            if ctx.toolbar_button(ui.Icon.SQUARE, label="Stop All"):
                self.stop_all()
            if ctx.toolbar_button(ui.Icon.TRASH, label="Delete", disabled=not self.selected, tone=ui.Tone.DANGER):
                self.ask_delete(list(self.selected))
            ctx.toolbar_separator()
            if ctx.toolbar_button(ui.Icon.INFO, label="Properties", disabled=len(self.selected) != 1):
                self.details_id = next(iter(self.selected))
                self.open_dialog("details")
            ctx.toolbar_spacer()
            if ctx.toolbar_button(ui.Icon.SETTINGS, label="Options"):
                self.open_dialog("options")

    def _categories(self, ctx: ui.UI, state: dict) -> None:
        tasks = state["downloads"]
        with ctx.column(width=ui.FILL, height=ui.FILL, bg=ui.Color.SURFACE_MUTED, pad=8, gap=2):
            with ctx.tree("Downloads", self.tree_open, icon=ui.Icon.FOLDER) as root:
                self.tree_open = root.open
                if root:
                    for key in ("all", "unfinished", "finished"):
                        count = len(tasks) if key == "all" else sum(
                            t["status"] == "completed" if key == "finished" else t["status"] != "completed" for t in tasks)
                        with ctx.tree(f"{CATEGORY_NAMES[key]} ({count})", leaf=True, icon=CATEGORY_ICONS[key],
                                      selected=self.category == key) as leaf:
                            if leaf.clicked:
                                self.category = key
                    with ctx.tree("Categories", self.categories_open, icon=ui.Icon.FOLDER) as group:
                        self.categories_open = group.open
                        if group:
                            for key in ("archive", "document", "audio", "video", "image", "other"):
                                count = sum(t["category"] == key for t in tasks)
                                with ctx.tree(f"{CATEGORY_NAMES[key]} ({count})", leaf=True, icon=CATEGORY_ICONS[key],
                                              selected=self.category == key) as leaf:
                                    if leaf.clicked:
                                        self.category = key

    def visible_tasks(self, state: dict) -> list[dict]:
        tasks = []
        for task in state["downloads"]:
            if self.category == "unfinished" and task["status"] == "completed":
                continue
            if self.category == "finished" and task["status"] != "completed":
                continue
            if self.category not in {"all", "unfinished", "finished"} and task["category"] != self.category:
                continue
            if self.search and self.search.lower() not in (task["name"] + " " + task["url"]).lower():
                continue
            tasks.append(task)
        keys = {0: lambda t: t["name"].lower(), 1: lambda t: t["size"], 2: lambda t: status_text(t),
                3: lambda t: percent(t), 4: lambda t: time_left(t), 5: lambda t: t["speed"],
                6: lambda t: t.get("completed_at") or t["created_at"], 7: lambda t: t["host"]}
        tasks.sort(key=keys.get(self.sort.column, keys[6]), reverse=self.sort.descending)
        return tasks

    def _download_table(self, ctx: ui.UI, state: dict) -> None:
        tasks = self.visible_tasks(state)
        columns = [ui.TableColumn("File Name", width=ui.GROW(1), sortable=True),
                   ui.TableColumn("Size", width=85, align=ui.TextAlign.RIGHT, sortable=True),
                   ui.TableColumn("Status", width=100, sortable=True),
                   ui.TableColumn("Progress", width=115, sortable=True),
                   ui.TableColumn("Time Left", width=85, sortable=True),
                   ui.TableColumn("Transfer Rate", width=105, align=ui.TextAlign.RIGHT, sortable=True),
                   ui.TableColumn("Last Try Date", width=135, sortable=True),
                   ui.TableColumn("Description", width=ui.GROW(1), sortable=True)]
        with ctx.table("downloads", columns, sort=self.sort, height=ui.FILL, compact=True,
                       striped=True, selectable=True) as table:
            if table:
                for task in tasks:
                    with ctx.id_scope(task["id"]):
                        clicked = table.row(task["id"] in self.selected)
                        if clicked:
                            if ui.Mod.PRIMARY in ctx.modifiers:
                                self.selected.symmetric_difference_update({task["id"]})
                            else:
                                self.selected = {task["id"]}
                            now = time.monotonic()
                            if self.last_row == task["id"] and now - self.last_click < 0.4:
                                if task["status"] == "completed":
                                    self.perform("open", {"id": task["id"]})
                                else:
                                    self.details_id = task["id"]
                                    self.open_dialog("details")
                            self.last_row, self.last_click = task["id"], now
                        with ctx.context_menu("row_actions") as menu:
                            if menu:
                                self.selected = {task["id"]}
                                if ctx.menu_item("Resume", icon=ui.Icon.PLAY):
                                    self.perform("action", {"id": task["id"], "action": "resume"})
                                if ctx.menu_item("Stop", icon=ui.Icon.PAUSE):
                                    self.perform("action", {"id": task["id"], "action": "pause"})
                                if ctx.menu_item("Remove…", icon=ui.Icon.TRASH, danger=True):
                                    self.ask_delete([task["id"]])
                                if ctx.menu_item("Properties", icon=ui.Icon.INFO):
                                    self.details_id = task["id"]
                                    self.open_dialog("details")
                        table.cell()
                        ctx.icon(CATEGORY_ICONS.get(task["category"], ui.Icon.FILE), size=15, color=ui.Color.ACCENT)
                        ctx.text(task["name"], size=12, nowrap=True)
                        table.cell_text(format_size(task["size"], estimated=bool(task.get("size_estimated"))))
                        table.cell_text(status_text(task))
                        table.cell()
                        ctx.progress(percent(task) / 100, width=ui.FILL, height=12,
                                     tone=ui.Tone.SUCCESS if task["status"] == "completed" else ui.Tone.ACCENT,
                                     indeterminate=task["status"] == "downloading" and not percent(task))
                        table.cell_text(time_left(task))
                        table.cell_text(f"{format_bytes(task['speed'])}/s" if task["speed"] else "—")
                        stamp = task.get("completed_at") or task["created_at"]
                        table.cell_text(time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)))
                        table.cell_text(("Local sample · " if task["source"] == "demo" else "") +
                                        (task.get("provider") or task["host"]))
            self.sort = table.sort or self.sort
        if not tasks:
            # The header remains visible; no promotional empty-state cards.
            ctx.text("No downloads in this view. Use Add URL or Spotify on the toolbar.", size=12,
                     color=ui.Color.TEXT_SECONDARY)
        self.selected.intersection_update({t["id"] for t in state["downloads"]})

    def ask_delete(self, ids: list[str]) -> None:
        self.delete_ids = ids
        self.open_dialog("delete")

    def paste_url(self, ctx: ui.UI) -> None:
        """Ctrl+V fills the active dialog, or opens the right one when none is open."""
        text = ctx.get_clipboard().strip()
        match = re.fullmatch(r"https?://\S+", text, re.IGNORECASE)
        if not match:
            message = "Copy a single HTTP or HTTPS link, then press Ctrl+V."
            if self.dialog:
                self.error = message
            else:
                self.notice = message
            return
        if self.dialog == "media":
            self.media_url = text
            self.media_result = None
            self.error = ""
            return
        if self.dialog == "spotify":
            self.spotify_url = text
            self.spotify_result = None
            self.spotify_selected.clear()
            self.error = ""
            return
        if self.dialog == "add":
            self.url = text
            self.error = ""
            return
        from .spotify import supported_url
        if supported_url(text) or urlsplit(text).hostname == "spotify.link":
            self.open_dialog("spotify")
            self.spotify_url = text
            self.spotify_result = None
            self.spotify_selected.clear()
        else:
            self.open_dialog("add")
            self.url = text
        self.notice = "Clipboard link pasted. Review the destination, then start the download."

    def _capture_clipboard(self, ctx: ui.UI, settings: dict) -> None:
        if not (settings["auto_capture"] and settings["clipboard_capture"]):
            return
        if time.monotonic() - self.last_clipboard_check < 2:
            return
        self.last_clipboard_check = time.monotonic()
        text = ctx.get_clipboard().strip()
        if text != self.last_clipboard:
            self.last_clipboard = text
            if text.startswith(("http://", "https://")) and "\n" not in text and Path(urlsplit(text).path).suffix.lower() in MEDIA_EXTENSIONS:
                try:
                    self.call("capture", {"url": text, "source": "clipboard"})
                except (OSError, ValueError, RuntimeError, EOFError):
                    pass

    def _error_in_dialog(self, ctx: ui.UI) -> None:
        if self.error:
            # Keep dialog errors visible until explicitly dismissed; an auto-
            # dismissible alert can disappear in the same click that caused it.
            ctx.alert(self.error[:450], tone=ui.Tone.DANGER)
            if ctx.button("Dismiss error", size=ui.ControlSize.SM):
                self.error = ""

    def _dialogs(self, ctx: ui.UI, state: dict) -> None:
        if self.dialog == "add":
            self._add_dialog(ctx)
        elif self.dialog == "spotify":
            self._spotify_dialog(ctx, state)
        elif self.dialog == "media":
            self._media_dialog(ctx, state)
        elif self.dialog == "options":
            self._options_dialog(ctx, state)
        elif self.dialog == "details":
            self._details_dialog(ctx, state)
        elif self.dialog == "library":
            self._library_dialog(ctx)
        elif self.dialog in {"delete", "shutdown", "about"}:
            title = {"delete": "Remove downloads", "shutdown": "Stop the background daemon?", "about": "About NDM"}[self.dialog]
            with ctx.dialog(title, True, width=480) as dialog:
                if dialog:
                    if self.dialog == "delete":
                        ctx.text(f"Remove {len(self.delete_ids)} queue entries? Unfinished files are deleted. Completed files stay on disk.", width=ui.FILL)
                        with ctx.dialog_footer():
                            if ctx.button("Cancel"):
                                self.dialog = ""
                            if ctx.button("Remove", variant=ui.ButtonVariant.DANGER):
                                for task_id in self.delete_ids:
                                    self.perform("action", {"id": task_id, "action": "delete"})
                                self.selected.difference_update(self.delete_ids)
                                self.dialog = ""
                    elif self.dialog == "shutdown":
                        ctx.text("Active transfers will stop and save their resumable state. Automatic capture will also stop.", width=ui.FILL)
                        with ctx.dialog_footer():
                            if ctx.button("Cancel"):
                                self.dialog = ""
                            if ctx.button("Stop daemon and exit", variant=ui.ButtonVariant.DANGER):
                                try:
                                    self.call("shutdown")
                                    self.app.quit()
                                except (OSError, ValueError, RuntimeError, EOFError) as exc:
                                    self.error = safe_error(exc, self.state.get("settings", {}))
                    else:
                        ctx.heading("NDM 0.1.0", level=3)
                        ctx.text("Native Python download manager · Nexa UI", width=ui.FILL)
                        ctx.text("Segmented HTTP downloads, yt-dlp media extraction, and spotDL matching. No web interface or listening web server.", width=ui.FILL)
                        ctx.text("spotDL uses Spotify metadata and matching public audio sources; it does not extract Spotify's protected streams.", width=ui.FILL, color=ui.Color.TEXT_SECONDARY)
                        with ctx.dialog_footer():
                            if ctx.button("Close"):
                                self.dialog = ""
                if not dialog.open:
                    self.dialog = ""

    def _add_dialog(self, ctx: ui.UI) -> None:
        with ctx.dialog("Add New Download", True, width=590) as dialog:
            if dialog:
                self.url = ctx.input("address", self.url, label="Address (URL)", width=ui.FILL, max_bytes=8192, autofocus=True)
                self.filename = ctx.input("save_as", self.filename, label="Save as (optional)", width=ui.FILL)
                self.add_category = ctx.select("category", ["Detect automatically", "Video", "Music", "Compressed", "Images", "Documents", "Other"], self.add_category, label="Category", width=ui.FILL)
                self.add_connections = int(ctx.number("connections", self.add_connections, 1, 8, label="Connections for this file", width=ui.FILL))
                self.start_paused = ctx.checkbox("Add to queue without starting", self.start_paused)
                ctx.text("For Spotify links, use the Spotify toolbar button instead.", size=11, color=ui.Color.TEXT_SECONDARY)
                self._error_in_dialog(ctx)
                with ctx.dialog_footer():
                    if ctx.button("Cancel"):
                        self.dialog = ""
                    if ctx.button("Start Download", icon=ui.Icon.DOWNLOAD, variant=ui.ButtonVariant.PRIMARY):
                        self.add()
            if not dialog.open:
                self.dialog = ""

    def _spotify_dialog(self, ctx: ui.UI, state: dict) -> None:
        info = state["daemon"]["spotify"]
        with ctx.dialog("Download Spotify Link — spotDL", True, width=820) as dialog:
            if dialog:
                ctx.text("Paste a public Spotify track, album, or playlist link. NDM previews the metadata before queuing individual tracks.", width=ui.FILL, size=12)
                self.spotify_url = ctx.input("spotify_address", self.spotify_url, label="Spotify link", placeholder="https://open.spotify.com/track/", width=ui.FILL, max_bytes=2048, autofocus=True)
                self.start_paused = ctx.checkbox("Add tracks without starting", self.start_paused)
                with ctx.row(gap=10, align=ui.Align.CENTER):
                    if ctx.button("Finding tracks…" if self.busy == "spotify" else "Find Tracks", icon=ui.Icon.SEARCH,
                                  variant=ui.ButtonVariant.PRIMARY, disabled=bool(self.busy)):
                        self.search_spotify()
                    ctx.text(f"Output: {state['settings']['spotify_format'].upper()} · {state['settings']['spotify_bitrate']} · FFmpeg {'ready' if info['ffmpeg'] else 'required'}", size=11)
                ctx.text("Audio source: YouTube Music via spotDL. Matches may differ from the original recording; this is not Spotify protected audio.", size=11, color=ui.Color.TEXT_SECONDARY, width=ui.FILL)
                if not info["installed"]:
                    ctx.alert("spotDL is missing. Run uv sync or pip install -e . to reinstall NDM's dependencies.", tone=ui.Tone.WARNING)
                self._error_in_dialog(ctx)
                if self.busy == "spotify":
                    elapsed = max(0, int(time.monotonic() - self.spotify_started))
                    ctx.progress(indeterminate=True, width=ui.FILL, label="Reading Spotify list")
                    ctx.text(f"Working… {elapsed}s elapsed. Fetching Spotify metadata for each track; large playlists can take a little while.",
                             size=11, color=ui.Color.TEXT_SECONDARY, width=ui.FILL)
                if self.spotify_result:
                    items = self.spotify_result["items"]
                    total = self.spotify_result.get("total", len(items))
                    title = self.spotify_result.get("title") or ""
                    found = f"Found {len(items)} track" + ("" if len(items) == 1 else "s")
                    if title:
                        found += f" — {title}"
                    ctx.text(found, size=13, font=ui.FontRole.MEDIUM, width=ui.FILL)
                    if self.spotify_result["truncated"]:
                        detail = f"Showing the first {len(items)} of {total} tracks." if total > len(items) else f"Showing the first {len(items)} tracks."
                        ctx.alert(detail + " Queue these, then import the remaining playlist separately.", tone=ui.Tone.WARNING)
                    columns = [ui.TableColumn("Title", width=ui.GROW(1)), ui.TableColumn("Artist", width=180),
                               ui.TableColumn("Album", width=180), ui.TableColumn("Duration", width=75)]
                    with ctx.table("spotify_tracks", columns, height=245, compact=True, striped=True, selectable=True) as table:
                        if table:
                            for item in items:
                                with ctx.id_scope(item["id"]):
                                    if table.row(item["id"] in self.spotify_selected):
                                        self.spotify_selected.symmetric_difference_update({item["id"]})
                                    table.cell_text(item["name"])
                                    table.cell_text(item["artist"])
                                    table.cell_text(item["album"])
                                    duration = item["duration"]
                                    table.cell_text(f"{duration // 60}:{duration % 60:02}")
                    ctx.text(f"{len(self.spotify_selected)} of {len(items)} tracks selected", size=11)
                with ctx.dialog_footer():
                    if ctx.button("Close"):
                        self.dialog = ""
                    if self.spotify_result:
                        if ctx.button("Select All"):
                            self.spotify_selected = {i["id"] for i in self.spotify_result["items"]}
                        if ctx.button("Queue Selected", icon=ui.Icon.DOWNLOAD, variant=ui.ButtonVariant.PRIMARY):
                            self.queue_spotify()
            if not dialog.open:
                self.dialog = ""

    def _media_dialog(self, ctx: ui.UI, state: dict) -> None:
        ffmpeg_ready = bool(state.get("daemon", {}).get("ffmpeg"))
        if not ffmpeg_ready:
            self.media_convert_mp3 = False
        with ctx.dialog("Media Grabber", True, width=740) as dialog:
            if dialog:
                self.media_url = ctx.input("media_address", self.media_url, label="Page or media URL",
                                           placeholder="https://example.com/video or direct media link",
                                           width=ui.FILL, max_bytes=8192, autofocus=True)
                if ctx.button("Inspecting…" if self.busy == "media" else "Find Media", icon=ui.Icon.SEARCH,
                              variant=ui.ButtonVariant.PRIMARY, disabled=bool(self.busy)):
                    self.inspect_media()
                self.media_convert_mp3 = ctx.checkbox("Convert to MP3 (extract audio, 192 kbps)",
                                                      self.media_convert_mp3, disabled=not ffmpeg_ready)
                if not ffmpeg_ready:
                    ctx.text("Install FFmpeg on PATH to enable MP3 conversion and video/audio merging.",
                             width=ui.FILL, size=11, color=ui.Color.TEXT_SECONDARY)
                elif self.media_convert_mp3:
                    ctx.text("Each Download button saves an MP3 audio file instead of the original video.",
                             width=ui.FILL, size=11, color=ui.Color.TEXT_SECONDARY)
                self._error_in_dialog(ctx)
                if self.media_result is None:
                    ctx.text("Direct media, HTML media elements, and sites supported by yt-dlp. Protected streams are not bypassed.", width=ui.FILL, size=11, color=ui.Color.TEXT_SECONDARY)
                if self.media_result:
                    result = self.media_result
                    ctx.text(result.get("title", "Media"), size=13, font=ui.FontRole.MEDIUM)
                    if result.get("message"):
                        ctx.text(result["message"], width=ui.FILL, size=11)
                    if result["items"]:
                        ctx.text("Sizes: exact when reported by the source, ~ when estimated, Unknown when unavailable. Estimates are replaced by the real file size on completion.",
                                 width=ui.FILL, size=10, color=ui.Color.TEXT_SECONDARY)
                    with ctx.scroll("media_results", height=260, width=ui.FILL, gap=5):
                        for item in result["items"]:
                            with ctx.id_scope(item["id"]), ctx.row(width=ui.FILL, gap=8, align=ui.Align.CENTER):
                                with ctx.column(width=ui.GROW(1), gap=2):
                                    ctx.text(item["name"] + (".mp3" if self.media_convert_mp3 else "." + item["ext"]), size=12, nowrap=True)
                                    ctx.text(item["quality"] + " · " + media_size_label(item, convert_mp3=self.media_convert_mp3) + " · " + item["note"], size=10, color=ui.Color.TEXT_SECONDARY)
                                if ctx.button("Download MP3" if self.media_convert_mp3 else "Download", size=ui.ControlSize.SM):
                                    mp3_size = mp3_size_estimate(item)
                                    self.perform("media.download", {"url": item.get("page_url") or item["url"],
                                                 "format_id": item["id"], "name": item["name"], "category": item["category"],
                                                 "quality": "audio" if self.media_convert_mp3 else "best",
                                                 "ext": "mp3" if self.media_convert_mp3 else item["ext"],
                                                 "size": mp3_size if self.media_convert_mp3 else item.get("size") or 0,
                                                 "size_estimated": True if self.media_convert_mp3 else bool(item.get("size_estimated"))})
                                    if not self.error:
                                        self.notice = "MP3 conversion queued." if self.media_convert_mp3 else "Media added to the download list."
                                        self.dialog = ""
                with ctx.dialog_footer():
                    if ctx.button("Close"):
                        self.dialog = ""
            if not dialog.open:
                self.dialog = ""

    def _proxy_options(self, ctx: ui.UI, form: dict) -> None:
        form["proxy_enabled"] = ctx.switch("Use a proxy for downloads and media lookups", form["proxy_enabled"])
        form["proxy_url"] = ctx.input("proxy_url", form["proxy_url"], label="Proxy URL",
                                      placeholder="http://proxy.local:8080 or socks5://127.0.0.1:1080",
                                      width=ui.FILL, max_bytes=2048, disabled=not form["proxy_enabled"])
        form["proxy_username"] = ctx.input("proxy_username", form["proxy_username"], label="Username (optional)",
                                           width=ui.FILL, max_bytes=256, disabled=not form["proxy_enabled"])
        form["proxy_password"] = ctx.input("proxy_password", form["proxy_password"], label="Password (optional)",
                                           password=True, width=ui.FILL, max_bytes=256, disabled=not form["proxy_enabled"])
        form["proxy_remote_dns"] = ctx.checkbox("Resolve hostnames through the SOCKS5 proxy (recommended)",
                                                form["proxy_remote_dns"], disabled=not form["proxy_enabled"])
        ctx.text("Supports HTTP, HTTPS, SOCKS5, and SOCKS5H. An enabled proxy overrides system proxy environment variables and is used for direct downloads, yt-dlp, and spotDL.", width=ui.FILL, size=11)
        ctx.text("Proxy credentials are stored in this local NDM state directory. Use a trusted proxy; HTTPS certificate verification stays enabled.", width=ui.FILL, size=11, color=ui.Color.TEXT_SECONDARY)

    def _appearance_options(self, ctx: ui.UI, state: dict) -> None:
        settings = state["settings"]
        changes: dict = {}
        overrides = dict(settings.get("colors", {}))
        modes = ["light", "dark", "system"]
        chosen = ctx.segmented("theme", ["Light", "Dark", "System"], modes.index(settings["theme"]))
        if modes[chosen] != settings["theme"]:
            changes["theme"] = modes[chosen]
        with ctx.row(gap=8, align=ui.Align.CENTER):
            ctx.text("Base accent", size=12)
            self.custom_color = ctx.color_picker("accent", self.custom_color)
            if ctx.changed:
                changes["accent"] = hex_color(self.custom_color, keep_alpha=False)
            if ctx.button("Reset palette"):
                self.color_hex = {}
                overrides.clear()
                changes["colors"] = {}
        with ctx.row(gap=8, wrap=True):
            for accent in ACCENTS:
                if ctx.button("##" + accent, color=accent, tooltip=accent):
                    changes["accent"] = accent
                    self.custom_color = ui.Color.hex(accent)
        ctx.text("Custom palette — every color below overrides the base theme. Valid changes apply immediately.", size=11, width=ui.FILL)
        groups = [
            ("Surfaces & text", ["background", "surface", "surface_raised", "surface_muted", "foreground", "text_secondary", "text_muted"]),
            ("Borders & interaction", ["border", "border_subtle", "border_strong", "hover", "active", "scrim", "focus_ring"]),
            ("Accent & status", ["accent", "accent_strong", "accent_subtle", "accent_foreground", "success", "warning", "danger", "info"]),
            ("Controls", ["control_on", "control_glyph", "control_track", "control_track_hover", "control_thumb", "control_thumb_on"]),
            ("Chart series", [f"series_{i}" for i in range(1, 7)]),
        ]
        colors_changed = False
        with ctx.scroll("custom_palette", height=235, width=ui.FILL, gap=6):
            for title, keys in groups:
                ctx.text(title, size=12, font=ui.FontRole.MEDIUM)
                for key in keys:
                    token = ui.Token[key.upper()]
                    current = overrides.get(key)
                    value = ui.Color.hex(current) if current else ctx.theme[token]
                    label = key.replace("_", " ").capitalize()
                    with ctx.id_scope(key), ctx.row(width=ui.FILL, gap=8, align=ui.Align.CENTER):
                        ctx.text(label, size=11, width=145)
                        picked = ctx.color_picker("picker", value)
                        if ctx.changed:
                            # Nexa's picker edits RGB only; keep any alpha the
                            # user already set in the hex field instead of
                            # silently making a translucent token opaque.
                            picked_hex = hex_color(picked, keep_alpha=False)
                            if value.a != 255:
                                picked_hex += f"{value.a:02x}"
                            overrides[key] = picked_hex
                            self.color_hex[key] = picked_hex
                            colors_changed = True
                        draft = self.color_hex.get(key, current or hex_color(value))
                        edited = ctx.input("hex", draft, width=115, max_bytes=9)
                        if ctx.changed:
                            self.color_hex[key] = edited
                            try:
                                normalized = normalize_color(edited)
                            except ValueError:
                                pass
                            else:
                                self.color_hex.pop(key, None)
                                if normalized != current:
                                    overrides[key] = normalized
                                    colors_changed = True
                        if ctx.button("Reset", size=ui.ControlSize.SM,
                                      disabled=key not in overrides and key not in self.color_hex):
                            overrides.pop(key, None)
                            self.color_hex.pop(key, None)
                            colors_changed = True
        if colors_changed:
            changes["colors"] = overrides
        ctx.text(f"{len(overrides)} custom colors · hex values accept #RRGGBB or #RRGGBBAA", size=10, color=ui.Color.TEXT_SECONDARY)
        # One IPC write per frame at most, and only when a widget actually
        # changed. The old code compared stale hex drafts every frame and could
        # hammer the daemon while merely scrolling through the palette.
        if changes:
            self.perform("settings", changes)

    def _options_dialog(self, ctx: ui.UI, state: dict) -> None:
        form = self.settings_form
        with ctx.dialog("NDM Options", True, width=720) as dialog:
            if dialog:
                self.options_tab = ctx.tabs("options_tabs", ["Connection", "Proxy", "Save To", "Capture", "Spotify", "Appearance"], self.options_tab)
                with ctx.scroll("options_body", width=ui.FILL, height=355, gap=13, pad=4):
                    if self.options_tab == 0:
                        form["max_concurrent"] = int(ctx.number("max_jobs", form["max_concurrent"], 1, 8, label="Maximum simultaneous downloads", width=ui.FILL))
                        form["connections"] = int(ctx.number("default_connections", form["connections"], 1, 8, label="Default connections per file", width=ui.FILL))
                        form["speed_limit"] = int(ctx.number("rate_limit", form["speed_limit"] / 1_000_000, 0, 100, step=.1, decimals=1, label="Download speed limit (MB/s; 0 = unlimited)", width=ui.FILL) * 1_000_000)
                        form["resume_on_start"] = ctx.checkbox("Resume interrupted downloads when the daemon starts", form["resume_on_start"])
                        form["notifications"] = ctx.checkbox("Notify when a download finishes", form["notifications"])
                    elif self.options_tab == 1:
                        self._proxy_options(ctx, form)
                    elif self.options_tab == 2:
                        form["download_dir"] = ctx.input("download_folder", form["download_dir"], label="Default download directory", width=ui.FILL)
                        if ctx.button("Browse…", icon=ui.Icon.FOLDER):
                            path = self.app.select_folder_dialog(title="Choose download folder")
                            if path:
                                form["download_dir"] = path
                        form["music_dir"] = ctx.input("music_folder", form["music_dir"], label="Existing music folder", width=ui.FILL)
                        if ctx.button("Browse music folder…", icon=ui.Icon.MUSIC):
                            path = self.app.select_folder_dialog(title="Choose music folder")
                            if path:
                                form["music_dir"] = path
                        ctx.text("Files are organized by category. Removing a completed queue entry keeps its file.", width=ui.FILL, size=11)
                    elif self.options_tab == 3:
                        form["auto_capture"] = ctx.switch("Enable automatic media capture", form["auto_capture"])
                        form["clipboard_capture"] = ctx.checkbox("Capture copied direct media links while this window is open", form["clipboard_capture"])
                        form["watch_inbox"] = ctx.checkbox("Watch the link inbox in the background", form["watch_inbox"])
                        ctx.text("Inbox: " + state["daemon"]["inbox"], selectable=True, size=11, width=ui.FILL)
                        domains = ctx.input("capture_domains", ", ".join(form["allowed_domains"]), label="Allowed hostnames (comma-separated; empty = any)", width=ui.FILL)
                        form["allowed_domains"] = [v.strip() for v in domains.split(",") if v.strip()]
                        ctx.text("Drop .txt or .url files containing media links into the inbox. No browser extension is required.", width=ui.FILL, size=11)
                        form["firefox_cookies"] = ctx.checkbox("Automatically use my Firefox cookies", form["firefox_cookies"])
                        ctx.text("Reads Firefox cookies locally for each new download, media inspection, or spotDL audio match. Only cookies matching the destination are sent; NDM does not save cookie values.", width=ui.FILL, size=11, color=ui.Color.TEXT_SECONDARY)
                    elif self.options_tab == 4:
                        formats = ["mp3", "m4a", "flac", "opus", "ogg"]
                        rates = ["auto", "128k", "192k", "256k", "320k", "disable"]
                        form["spotify_format"] = formats[ctx.select("spotify_format", [v.upper() for v in formats], formats.index(form["spotify_format"]), label="Audio file format", width=ui.FILL)]
                        form["spotify_bitrate"] = rates[ctx.select("spotify_bitrate", rates, rates.index(form["spotify_bitrate"]), label="Conversion bitrate (auto preserves source bitrate)", width=ui.FILL)]
                        info = state["daemon"]["spotify"]
                        ctx.text(f"spotDL {info['version'] or 'not installed'} · FFmpeg {'installed' if info['ffmpeg'] else 'missing'}", size=12)
                        ctx.text("NDM calls the upstream spotDL Python API. Spotify supplies metadata; audio comes from a matching YouTube Music result. No spotDL web UI is launched.", width=ui.FILL, size=11)
                        ctx.text("The Capture tab's Firefox cookie option also applies to spotDL's YouTube Music search and audio download requests.", width=ui.FILL, size=11, color=ui.Color.TEXT_SECONDARY)
                        ctx.text("Use only audio you own or have permission to save. Pausing a spotDL track is cooperative; conversion may finish before it stops.", width=ui.FILL, size=11, color=ui.Color.TEXT_SECONDARY)
                    else:
                        self._appearance_options(ctx, state)
                self._error_in_dialog(ctx)
                with ctx.dialog_footer():
                    if ctx.button("Cancel"):
                        self.dialog = ""
                    if ctx.button("OK", icon=ui.Icon.CHECK, variant=ui.ButtonVariant.PRIMARY):
                        self.save_options()
            if not dialog.open:
                self.dialog = ""

    def _details_dialog(self, ctx: ui.UI, state: dict) -> None:
        task = next((t for t in state["downloads"] if t["id"] == self.details_id), None)
        with ctx.dialog("Download Properties", bool(task), width=650) as dialog:
            if dialog and task:
                for label, value in [("File name", task["name"]), ("Address", task["url"]), ("Saved to", task["path"]),
                                     ("Status", status_text(task)),
                                     ("Size", format_size(task["size"], estimated=bool(task.get("size_estimated")))),
                                     ("Downloaded", format_bytes(task["downloaded"])), ("Source", task.get("provider") or task["host"]),
                                     ("Connections", str(len(task["segments"]) or task["connections"])),
                                     ("Range resume", "Supported" if task["resumable"] else "Not used for this transfer")]:
                    with ctx.row(width=ui.FILL, gap=12):
                        ctx.text(label + ":", size=11, color=ui.Color.TEXT_SECONDARY, width=105)
                        ctx.text(value, size=11, selectable=True, width=ui.GROW(1))
                ctx.progress(percent(task) / 100, show_value=True, width=ui.FILL)
                if task["error"]:
                    ctx.alert(task["error"], tone=ui.Tone.DANGER)
                with ctx.dialog_footer():
                    if ctx.button("Close"):
                        self.dialog = ""
                    if task["status"] == "completed":
                        if ctx.button("Show in Folder", icon=ui.Icon.FOLDER):
                            self.perform("open", {"id": task["id"], "folder": True})
                        if ctx.button("Open File", icon=ui.Icon.EXTERNAL_LINK, variant=ui.ButtonVariant.PRIMARY):
                            self.perform("open", {"id": task["id"]})
                    elif ctx.button("Stop" if task["status"] in {"queued", "downloading"} else "Resume", variant=ui.ButtonVariant.PRIMARY):
                        self.perform("action", {"id": task["id"], "action": "pause" if task["status"] in {"queued", "downloading"} else "resume"})
            if not dialog.open:
                self.dialog = ""

    def _library_dialog(self, ctx: ui.UI) -> None:
        with ctx.dialog("Offline Audio Files", True, width=760) as dialog:
            if dialog:
                with ctx.row(gap=8):
                    if ctx.button("Refresh", icon=ui.Icon.REFRESH_CW):
                        self.load_library()
                    if ctx.button("Export M3U Playlist", icon=ui.Icon.SAVE):
                        path = self.app.save_file_dialog(title="Export playlist", filter_name="M3U playlist", filter_ext="m3u")
                        if path:
                            try:
                                Path(path).write_text(self.call("playlist")["content"], encoding="utf-8")
                            except (OSError, ValueError, RuntimeError) as exc:
                                self.error = safe_error(exc, self.state.get("settings", {}))
                self._error_in_dialog(ctx)
                columns = [ui.TableColumn("File", width=ui.GROW(1)), ui.TableColumn("Size", width=90), ui.TableColumn("Folder", width=280)]
                with ctx.table("local_audio", columns, height=330, compact=True, striped=True, selectable=True) as table:
                    if table:
                        for index, track in enumerate(self.library):
                            with ctx.id_scope(str(index)):
                                if table.row(index == self.library_selection):
                                    self.library_selection = index
                                    try:
                                        os_open_path(track["path"])
                                    except OSError as exc:
                                        self.error = safe_error(exc, self.state.get("settings", {}))
                                table.cell_text(track["name"])
                                table.cell_text(format_bytes(track["size"]))
                                table.cell_text(track["folder"])
                ctx.text("Click a file to open it in your desktop audio player. Files stay on this computer.", size=11, color=ui.Color.TEXT_SECONDARY)
                with ctx.dialog_footer():
                    if ctx.button("Close"):
                        self.dialog = ""
            if not dialog.open:
                self.dialog = ""

    def run(self) -> None:
        settings = self.state["settings"]
        with ui.App("NDM — Download Manager", width=1280, height=780, min_width=980, min_height=560,
                    theme=ui.ThemeMode.DARK if settings["theme"] == "dark" else ui.ThemeMode.LIGHT) as app:
            self.app = app
            self.runner = ui.BackgroundRunner.for_app(app, max_workers=3)
            threading.Thread(target=self._background, name="ndm-desktop-state", daemon=True).start()
            try:
                app.run(self.draw)
            finally:
                self.stop.set()
                self.runner.close()
                # Closing the window intentionally leaves the native daemon alive.


def state_has_tasks(state: dict) -> bool:
    return bool(state["downloads"])


def os_open_path(path: str) -> None:
    import subprocess
    import sys
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
