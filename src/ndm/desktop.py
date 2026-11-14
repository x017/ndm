"""Native Nexa UI. This is the real nexa-ui desktop frontend, not a webview."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import nexa as ui

from .models import ACCENTS, MEDIA_EXTENSIONS


def format_bytes(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return "0 B"


class Desktop:
    def __init__(self, *, port: int = 8765, data_dir: Path, screenshot: str | None = None):
        self.base = f"http://127.0.0.1:{port}"
        self.data_dir = data_dir
        self.client = httpx.Client(base_url=self.base, headers={"X-NDM-Client": "1"}, timeout=20)
        self._ensure_daemon(port)
        self.state = self.client.get("/api/state").json()
        self.cache_lock = threading.RLock()
        self.stop = threading.Event()
        self.app = None
        self.page = "downloads"
        self.category = "all"
        self.tab = 0
        self.search = ""
        self.url = ""
        self.filename = ""
        self.add_open = False
        self.authorized = False
        self.start_paused = False
        self.add_connections = 4
        self.add_category = 0
        self.error = ""
        self.media_url = ""
        self.media_authorized = False
        self.media_result = None
        self.media_busy = False
        self.details_id = None
        self.delete_id = None
        self.last_clipboard = ""
        self.last_clipboard_check = 0.0
        self.completed_ids = {t["id"] for t in self.state["downloads"] if t["status"] == "completed"}
        self.settings_form = dict(self.state["settings"])
        self.library = []
        self.custom_color = ui.Color.hex(self.state["settings"]["accent"])
        self.screenshot = screenshot
        if screenshot:
            os.environ["NEXA_HEADLESS"] = "1"
            os.environ["CUI_SCREENSHOT"] = str(Path(screenshot).resolve())
            os.environ["CUI_SCREENSHOT_FRAMES"] = "12"
        self.runner = None
        self._following_system = False

    def _ensure_daemon(self, port: int) -> None:
        try:
            response = self.client.get("/api/health")
            if response.is_success and response.json().get("name") == "ndm":
                return
        except httpx.HTTPError:
            pass
        self.data_dir.mkdir(parents=True, exist_ok=True)
        log = (self.data_dir / "daemon.log").open("ab")
        kwargs = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
                  "env": {**os.environ, "NDM_HOME": str(self.data_dir)}}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, "-m", "ndm", "daemon", "--port", str(port)], **kwargs)
        log.close()
        for _ in range(100):
            time.sleep(0.1)
            try:
                if self.client.get("/api/health").is_success:
                    return
            except httpx.HTTPError:
                pass
        raise RuntimeError(f"The daemon did not start. See {self.data_dir / 'daemon.log'}.")

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        response = self.client.request(method, "/api" + path, json=payload)
        data = response.json()
        if not response.is_success:
            detail = data.get("detail", "Request failed.")
            if isinstance(detail, list):
                detail = " ".join(d.get("msg", "") for d in detail)
            raise ValueError(detail)
        return data

    def refresh(self) -> None:
        try:
            state = self.client.get("/api/state").json()
            with self.cache_lock:
                self.state = state
                self.error = ""
        except (httpx.HTTPError, ValueError) as exc:
            with self.cache_lock:
                self.error = f"Reconnecting to the daemon: {exc}"

    def _background(self) -> None:
        while not self.stop.wait(1):
            self.refresh()
            if self.app:
                self.app.wake()

    def navigate(self, page: str, category: str = "all") -> None:
        self.page, self.category, self.tab, self.search = page, category, 0, ""
        if page == "settings":
            self.settings_form = dict(self.state["settings"])
            self.custom_color = ui.Color.hex(self.state["settings"]["accent"])
        if page == "music":
            self.runner.submit(lambda: self.request("GET", "/library"), on_done=self._library_loaded,
                               on_error=lambda exc: setattr(self, "error", str(exc)))

    def _library_loaded(self, result) -> None:
        # BackgroundRunner results are drained by the UI thread in draw().
        if isinstance(result, dict):
            self.library = result.get("tracks", [])

    def action(self, task_id: str, action: str) -> None:
        try:
            self.request("POST", f"/downloads/{task_id}/{action}")
            self.refresh()
        except (ValueError, httpx.HTTPError) as exc:
            self.error = str(exc)

    def patch(self, values: dict) -> None:
        try:
            self.request("PATCH", "/settings", values)
            self.refresh()
        except (ValueError, httpx.HTTPError) as exc:
            self.error = str(exc)

    def add(self) -> None:
        try:
            categories = ["auto", "video", "audio", "archive", "image", "document", "other"]
            self.request("POST", "/downloads", {
                "url": self.url.strip(), "filename": self.filename.strip() or None,
                "category": None if self.add_category == 0 else categories[self.add_category],
                "connections": self.add_connections, "start_paused": self.start_paused,
                "authorized": self.authorized,
            })
            self.add_open = False
            self.url = self.filename = ""
            self.authorized = False
            self.error = ""
            self.navigate("downloads")
        except (ValueError, httpx.HTTPError) as exc:
            self.error = str(exc)

    def inspect(self) -> None:
        self.media_busy = True
        self.media_result = None
        url, authorized = self.media_url, self.media_authorized
        self.runner.submit(lambda: self.request("POST", "/media/inspect", {"url": url, "authorized": authorized}),
                           on_done=self._media_loaded, on_error=self._media_error)

    def _media_error(self, exc: BaseException) -> None:
        self.media_busy = False
        self.error = str(exc)

    def _media_loaded(self, result) -> None:
        self.media_busy = False
        if isinstance(result, dict):
            self.media_result = result
        else:
            self.error = str(result)

    def download_media(self, item: dict) -> None:
        try:
            if item.get("page_url"):
                self.request("POST", "/media/download", {"url": item["page_url"], "format_id": item["id"],
                             "name": item["name"], "category": item["category"], "ext": item["ext"],
                             "authorized": True})
            else:
                self.request("POST", "/downloads", {"url": item["url"], "filename": item["name"],
                             "category": item["category"], "authorized": True})
            self.refresh()
        except (ValueError, httpx.HTTPError) as exc:
            self.error = str(exc)

    def apply_theme(self, ctx: ui.UI, settings: dict) -> None:
        mode = settings["theme"]
        if mode == "system":
            if not self._following_system:
                try:
                    self.app.follow_system_theme(True)
                    self._following_system = True
                except RuntimeError:
                    ctx.theme_mode = ui.ThemeMode.LIGHT
        else:
            if self._following_system:
                self.app.follow_system_theme(False)
                self._following_system = False
            ctx.theme_mode = ui.ThemeMode.DARK if mode == "dark" else ui.ThemeMode.LIGHT
        accent = ui.Color.hex(settings["accent"])
        ctx.theme[ui.Token.ACCENT] = accent
        ctx.theme[ui.Token.ACCENT_STRONG] = accent
        ctx.theme[ui.Token.ACCENT_SUBTLE] = ui.with_alpha(accent, 0.12)
        ctx.theme[ui.Token.CONTROL_ON] = accent
        ctx.theme[ui.Token.FOCUS_RING] = ui.with_alpha(accent, 0.45)
        ctx.theme[ui.Token.SERIES_1] = accent
        luminance = accent.r * .299 + accent.g * .587 + accent.b * .114
        ctx.theme[ui.Token.ACCENT_FOREGROUND] = ui.Color.hex("#172033" if luminance > 165 else "#ffffff")
        ctx.theme.radius_control = 7
        ctx.theme.radius_surface = 11
        ctx.theme.radius_panel = 14

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
        if settings["auto_capture"] and settings["clipboard_capture"] and time.monotonic() - self.last_clipboard_check > 2:
            self.last_clipboard_check = time.monotonic()
            text = ctx.get_clipboard().strip()
            if text != self.last_clipboard:
                self.last_clipboard = text
                if text.startswith(("http://", "https://")) and "\n" not in text and Path(urlsplit(text).path).suffix.lower() in MEDIA_EXTENSIONS:
                    try:
                        self.request("POST", "/capture", {"url": text, "source": "clipboard", "authorized": True})
                    except (ValueError, httpx.HTTPError):
                        pass
        with ctx.row(width=ui.FILL, height=ui.FILL, bg=ui.Color.BACKGROUND):
            self._sidebar(ctx, state)
            with ctx.column(width=ui.FILL, height=ui.FILL):
                with ctx.row(width=ui.FILL, height=65, pad_x=25, gap=12, align=ui.Align.CENTER, bg=ui.Color.SURFACE):
                    ctx.secondary("Workspace")
                    ctx.icon(ui.Icon.CHEVRON_RIGHT, size=12, color=ui.Color.TEXT_MUTED)
                    ctx.text(self.page.title() if self.page != "music" else "Offline music", size=12)
                    ctx.spacer()
                    self.search = ctx.input("global_search", self.search, placeholder="Search downloads…", width=225)
                    if ctx.icon_button(ui.Icon.SEARCH, tooltip="Search downloads"):
                        self.navigate("downloads")
                    if ctx.shortcut(ui.Mod.PRIMARY, ui.Key.N):
                        self.add_open = True
                    if ctx.icon_button(ui.Icon.SUN if settings["theme"] == "dark" else ui.Icon.MOON,
                                       tooltip="Toggle light and dark theme"):
                        self.patch({"theme": "light" if settings["theme"] == "dark" else "dark"})
                ctx.separator()
                if self.error:
                    with ctx.row(width=ui.FILL, pad_x=25, pad_y=7):
                        if ctx.alert(self.error[:240], tone=ui.Tone.WARNING, dismissible=True):
                            self.error = ""
                with ctx.scroll("main_scroll", width=ui.FILL, height=ui.FILL, pad=26, gap=20):
                    with ctx.row(width=ui.FILL, align=ui.Align.CENTER):
                        with ctx.column(gap=6, width=ui.GROW(1)):
                            ctx.heading({"downloads": "Downloads.", "media": "Media grabber.", "music": "Offline music.", "settings": "Settings."}[self.page], level=1)
                            ctx.secondary({"downloads": "Everything you need. Nothing in the way.", "media": "From link to library. Always with your permission.", "music": "Your own sounds, ready when the internet isn’t.", "settings": "A workspace that works the way you do."}[self.page])
                        if self.page in {"downloads", "music"} and ctx.button("New download", icon=ui.Icon.PLUS, variant=ui.ButtonVariant.PRIMARY):
                            self.add_open = True
                    if self.page == "downloads":
                        self._downloads(ctx, state)
                    elif self.page == "media":
                        self._media(ctx, state)
                    elif self.page == "music":
                        self._music(ctx)
                    else:
                        self._settings(ctx, state)
                    with ctx.row(width=ui.FILL, gap=8, align=ui.Align.CENTER):
                        ctx.text("ndm.", font=ui.FontRole.MEDIUM, size=12, color=ui.Color.TEXT_MUTED)
                        ctx.text("Your downloads, in flow.", size=10, color=ui.Color.TEXT_MUTED)
                        ctx.spacer()
                        ctx.text("Native desktop · Nexa UI · v0.1.0", size=10, color=ui.Color.TEXT_MUTED)
        self._dialogs(ctx, state)

    def _sidebar(self, ctx: ui.UI, state: dict) -> None:
        with ctx.column(width=216, height=ui.FILL, bg=ui.Color.SURFACE_MUTED, pad=15, gap=5):
            with ctx.row(gap=10, pad=9, align=ui.Align.CENTER):
                with ctx.box(width=34, height=36, radius=10, bg=ui.Color.ACCENT, dir=ui.Direction.STACK,
                             align=ui.Align.CENTER, justify=ui.Justify.CENTER):
                    ctx.icon(ui.Icon.DOWNLOAD, size=22, color=ui.Color.ACCENT_FOREGROUND)
                ctx.heading("ndm.", level=2)
            ctx.gap(18)
            ctx.text("WORKSPACE", size=9, color=ui.Color.TEXT_MUTED)
            ctx.gap(5)
            for label, page, icon in [("Downloads", "downloads", ui.Icon.DOWNLOAD), ("Media grabber", "media", ui.Icon.LINK),
                                      ("Offline music", "music", ui.Icon.MUSIC), ("Settings", "settings", ui.Icon.SETTINGS)]:
                if ctx.nav_item(label, icon=icon, selected=self.page == page, group="main_nav",
                                badge=str(state["stats"]["total"]) if page == "downloads" else None):
                    self.navigate(page)
            ctx.gap(15)
            ctx.separator()
            ctx.gap(12)
            ctx.text("LIBRARY", size=9, color=ui.Color.TEXT_MUTED)
            for label, category, icon in [("All files", "all", ui.Icon.FOLDER), ("Videos", "video", ui.Icon.VIDEO),
                                          ("Audio", "audio", ui.Icon.MUSIC), ("Archives", "archive", ui.Icon.ARCHIVE),
                                          ("Images", "image", ui.Icon.IMAGE), ("Documents", "document", ui.Icon.FILE_TEXT)]:
                if ctx.nav_item(label, icon=icon, selected=self.page == "downloads" and self.category == category, group="library_nav"):
                    self.navigate("downloads", category)
            ctx.spacer()
            with ctx.card(pad=13, gap=9, width=ui.FILL, bg=ui.Color.ACCENT_SUBTLE):
                ctx.icon(ui.Icon.ACTIVITY, size=19, color=ui.Color.ACCENT)
                ctx.text("Quietly working for you.", size=11, font=ui.FontRole.MEDIUM)
                ctx.text("Your daemon keeps things moving, even when you step away.", size=10, color=ui.Color.TEXT_SECONDARY)
                ctx.badge("Engine online" if state["settings"]["engine_running"] else "Engine paused",
                          tone=ui.Tone.SUCCESS if state["settings"]["engine_running"] else ui.Tone.WARNING, dot=True)
            ctx.gap(8)
            ctx.text("Personal workspace · v0.1.0", size=9, color=ui.Color.TEXT_MUTED)

    def _downloads(self, ctx: ui.UI, state: dict) -> None:
        stats, settings = state["stats"], state["settings"]
        with ctx.card(width=ui.FILL, pad=17, gap=8, bg=ui.Color.ACCENT_SUBTLE, border_color=ui.Color.ACCENT_SUBTLE):
            with ctx.row(gap=13, align=ui.Align.CENTER):
                ctx.icon(ui.Icon.ZAP, size=26, color=ui.Color.ACCENT)
                with ctx.column(gap=5):
                    ctx.heading("Big files. Small effort.", level=4)
                    ctx.text("Smart segments, seamless resumes, and a quiet engine that keeps things moving.", size=11, color=ui.Color.TEXT_SECONDARY)
        with ctx.grid(columns=4, min_column_width=150, gap=14, width=ui.FILL):
            ctx.metric("Active downloads", str(stats["active"]), icon=ui.Icon.DOWNLOAD, delta_label=f"{stats['queued']} in queue")
            ctx.metric("Transfer speed", format_bytes(stats["speed"]) + "/s", icon=ui.Icon.ACTIVITY, spark=state["history"][-25:])
            ctx.metric("Completed today", str(stats["completed_today"]), icon=ui.Icon.CIRCLE_CHECK, delta_label=format_bytes(stats["bytes_today"]))
            ctx.metric("Total downloaded", format_bytes(stats["bytes_total"]), icon=ui.Icon.DATABASE, delta_label=format_bytes(stats["disk_free"]) + " free")
        with ctx.row(width=ui.FILL, gap=18, align=ui.Align.START):
            with ctx.column(width=ui.GROW(1), gap=13):
                self.tab = ctx.tabs("status_tabs", ["All downloads", "Active", "Queued", "Completed"], self.tab)
                status_filter = ["all", "downloading", "queued", "completed"][self.tab]
                tasks = [t for t in state["downloads"] if (self.category == "all" or t["category"] == self.category)
                         and (status_filter == "all" or t["status"] == status_filter)
                         and (not self.search or self.search.lower() in (t["name"] + t["host"]).lower())]
                tasks.sort(key=lambda t: ({"downloading": 0, "queued": 1, "paused": 2, "error": 3, "completed": 4}[t["status"]], -t["created_at"]))
                if not tasks and ctx.empty_state("Make room for something good", icon=ui.Icon.DOWNLOAD,
                                                 description="Add a link, or clear your filters to see more downloads.", action="Add a download"):
                    self.add_open = True
                for task in tasks:
                    self._download_row(ctx, task)
                ctx.text(f"{len(tasks)} downloads · Your queue runs automatically", size=10, color=ui.Color.TEXT_MUTED)
            with ctx.column(width=235, gap=15):
                with ctx.card(width=ui.FILL, pad=16, gap=14):
                    ctx.heading("Background engine", level=4)
                    running = ctx.switch("Always in motion", settings["engine_running"], description="Downloads keep going. You don’t have to.")
                    if running != settings["engine_running"]:
                        self.patch({"engine_running": running})
                    ctx.separator()
                    ctx.text(f"{stats['active']} / {settings['max_concurrent']} concurrent files", size=11)
                    ctx.text(f"{stats['connections']} active connections", size=11, color=ui.Color.TEXT_SECONDARY)
                    ctx.badge("Daemon online" if settings["engine_running"] else "Daemon paused", tone=ui.Tone.SUCCESS if settings["engine_running"] else ui.Tone.WARNING, dot=True)
                with ctx.card(width=ui.FILL, pad=16, gap=10):
                    ctx.heading("Transfer activity", level=4)
                    ctx.heading(format_bytes(stats["speed"]) + "/s", level=3)
                    ctx.line_chart("speed_chart", [ui.Series("Download speed", state["history"], color=settings["accent"])], height=125, area=True, smooth=True)
                    ctx.text("Last 60 seconds", size=9, color=ui.Color.TEXT_MUTED)
                with ctx.card(width=ui.FILL, pad=16, gap=10, bg=ui.Color.ACCENT_SUBTLE):
                    ctx.heading("A little more you.", level=4)
                    ctx.text("Your workspace, in your colors.", size=10, color=ui.Color.TEXT_SECONDARY)
                    if ctx.button("Customize appearance", icon=ui.Icon.PALETTE, variant=ui.ButtonVariant.GHOST):
                        self.navigate("settings")

    def _download_row(self, ctx: ui.UI, task: dict) -> None:
        icons = {"video": ui.Icon.VIDEO, "audio": ui.Icon.MUSIC, "archive": ui.Icon.ARCHIVE, "image": ui.Icon.IMAGE, "document": ui.Icon.FILE_TEXT}
        progress = min(100, task["downloaded"] / task["size"] * 100) if task["size"] else 0
        with ctx.id_scope(task["id"]):
            with ctx.card(width=ui.FILL, pad=13, gap=8):
                with ctx.row(width=ui.FILL, gap=11, align=ui.Align.CENTER):
                    with ctx.box(width=35, height=39, radius=8, bg=ui.Color.ACCENT_SUBTLE,
                                 dir=ui.Direction.STACK, align=ui.Align.CENTER, justify=ui.Justify.CENTER):
                        ctx.icon(icons.get(task["category"], ui.Icon.FILE), size=20, color=ui.Color.ACCENT)
                    with ctx.column(width=ui.GROW(1), gap=5):
                        ctx.text(task["name"], size=11, font=ui.FontRole.MEDIUM, nowrap=True)
                        ctx.text(("Local sample · " if task["source"] == "demo" else "") + task["host"], size=9, color=ui.Color.TEXT_MUTED, nowrap=True)
                    with ctx.column(width=133, gap=6):
                        ctx.progress(progress, tone=ui.Tone.SUCCESS if task["status"] == "completed" else ui.Tone.ACCENT, width=ui.FILL)
                        ctx.text(f"{format_bytes(task['downloaded'])} / {format_bytes(task['size'])}", size=8, color=ui.Color.TEXT_MUTED)
                    tones = {"downloading": ui.Tone.ACCENT, "completed": ui.Tone.SUCCESS, "paused": ui.Tone.NEUTRAL, "queued": ui.Tone.WARNING, "error": ui.Tone.DANGER}
                    ctx.badge(task["status"].title(), tone=tones[task["status"]], dot=True, size=ui.ControlSize.SM)
                    if task["status"] != "completed":
                        active = task["status"] in {"downloading", "queued"}
                        if ctx.icon_button(ui.Icon.PAUSE if active else ui.Icon.PLAY, tooltip="Pause" if active else "Resume"):
                            self.action(task["id"], "pause" if active else "retry" if task["status"] == "error" else "resume")
                    if ctx.icon_button(ui.Icon.ELLIPSIS, tooltip="Download details"):
                        self.details_id = task["id"]
                if task["status"] == "downloading":
                    ctx.text(f"{format_bytes(task['speed'])}/s · {task['connections']} connections", size=9, color=ui.Color.TEXT_MUTED)

    def _media(self, ctx: ui.UI, state: dict) -> None:
        settings = state["settings"]
        with ctx.card(width=ui.FILL, pad=22, gap=15):
            ctx.heading("There’s something good in that link.", level=3)
            ctx.secondary("Paste a video, audio, or webpage URL. We’ll look for downloadable media.")
            self.media_url = ctx.input("media_url", self.media_url, placeholder="https://…", width=ui.FILL, max_bytes=8192)
            self.media_authorized = ctx.checkbox("I own this content or have permission to save it", self.media_authorized)
            if ctx.button("Looking…" if self.media_busy else "Find media", icon=ui.Icon.SEARCH, variant=ui.ButtonVariant.PRIMARY, disabled=self.media_busy):
                self.inspect()
        if self.media_result:
            result = self.media_result
            with ctx.card(width=ui.FILL, pad=20, gap=13):
                ctx.heading(result.get("title", "Media"), level=3)
                if result.get("message"):
                    ctx.text(result["message"], size=12, color=ui.Color.TEXT_SECONDARY, width=ui.FILL)
                if result["kind"] == "spotify":
                    if ctx.button("Open in Spotify", icon=ui.Icon.EXTERNAL_LINK, variant=ui.ButtonVariant.PRIMARY):
                        webbrowser.open(self.media_url)
                for item in result["items"]:
                    with ctx.id_scope(item["id"]):
                        with ctx.row(width=ui.FILL, gap=12, align=ui.Align.CENTER):
                            ctx.icon(ui.Icon.VIDEO if item["category"] == "video" else ui.Icon.MUSIC, color=ui.Color.ACCENT)
                            with ctx.column(width=ui.GROW(1), gap=4):
                                ctx.text(item["name"] + "." + item["ext"], size=12)
                                ctx.text(item["quality"] + " · " + item["note"], size=10, color=ui.Color.TEXT_MUTED)
                            if ctx.button("Download", icon=ui.Icon.DOWNLOAD, size=ui.ControlSize.SM):
                                self.download_media(item)
        with ctx.card(width=ui.FILL, pad=20, gap=15):
            ctx.heading("Automatic capture, on your terms.", level=3)
            for label, key, description in [
                ("Enable media capture", "auto_capture", "Off by default. The daemon’s allowlist and media types apply."),
                ("Capture copied media links", "clipboard_capture", "Only while this desktop window is open. Requires media capture above."),
                ("Watch the link inbox", "watch_inbox", "Drop authorized links as .txt or .url files. Works without the window."),
            ]:
                value = ctx.switch(label, settings[key], description=description)
                if value != settings[key]:
                    self.patch({key: value})
            ctx.text("Inbox: " + state["daemon"]["inbox"], size=10, color=ui.Color.TEXT_MUTED, selectable=True)
            ctx.text("Browser companion setup and your private token are available in the browser companion’s Media grabber page.", size=11, color=ui.Color.TEXT_SECONDARY)
            if ctx.button("Open browser companion", icon=ui.Icon.EXTERNAL_LINK):
                webbrowser.open(self.base)

    def _music(self, ctx: ui.UI) -> None:
        with ctx.card(width=ui.FILL, pad=22, gap=12, bg=ui.Color.ACCENT_SUBTLE):
            ctx.heading("The soundtrack stays with you.", level=2)
            ctx.text("Your own audio files, ready when the internet isn’t. No uploads. No connection required.", size=12, color=ui.Color.TEXT_SECONDARY)
            with ctx.row(gap=10):
                if ctx.button("Refresh library", icon=ui.Icon.REFRESH_CW):
                    self.navigate("music")
                if ctx.button("Export playlist", icon=ui.Icon.DOWNLOAD):
                    path = self.app.save_file_dialog(title="Save offline playlist", filter_name="M3U playlist", filter_ext="m3u")
                    if path:
                        response = self.client.get("/library/playlist.m3u")
                        Path(path).write_bytes(response.content)
        if not self.library:
            ctx.empty_state("Your offline shelf is waiting", icon=ui.Icon.MUSIC, description="Download audio you own, or choose a music folder in Settings.")
        for track in self.library:
            if self.search and self.search.lower() not in track["name"].lower():
                continue
            with ctx.id_scope(track["id"]):
                with ctx.card(width=ui.FILL, pad=15):
                    with ctx.row(width=ui.FILL, gap=13, align=ui.Align.CENTER):
                        ctx.icon(ui.Icon.MUSIC, size=23, color=ui.Color.ACCENT)
                        with ctx.column(width=ui.GROW(1), gap=5):
                            ctx.text(track["name"], size=12)
                            ctx.text(track["folder"], size=9, color=ui.Color.TEXT_MUTED)
                        ctx.text(format_bytes(track["size"]), size=11, color=ui.Color.TEXT_SECONDARY)
                        if ctx.button("Play", icon=ui.Icon.PLAY, size=ui.ControlSize.SM):
                            webbrowser.open(Path(track["path"]).as_uri())

    def _settings(self, ctx: ui.UI, state: dict) -> None:
        form = self.settings_form
        with ctx.card(width=ui.FILL, pad=22, gap=17):
            ctx.heading("A place for everything.", level=3)
            form["download_dir"] = ctx.input("download_dir", form["download_dir"], label="Download folder", width=ui.FILL)
            if ctx.button("Browse folder", icon=ui.Icon.FOLDER):
                path = self.app.select_folder_dialog(title="Choose a download folder")
                if path:
                    form["download_dir"] = path
            form["music_dir"] = ctx.input("music_dir", form["music_dir"], label="Music library folder", width=ui.FILL)
            if ctx.button("Browse music folder", icon=ui.Icon.MUSIC):
                path = self.app.select_folder_dialog(title="Choose your music folder")
                if path:
                    form["music_dir"] = path
            with ctx.row(width=ui.FILL, gap=18):
                form["max_concurrent"] = int(ctx.number("max_concurrent", form["max_concurrent"], 1, 8, label="Simultaneous downloads", width=ui.GROW(1)))
                form["connections"] = int(ctx.number("connections", form["connections"], 1, 8, label="Connections per file", width=ui.GROW(1)))
            limit = ctx.number("speed_limit", form["speed_limit"] / 1_000_000, 0, 100, step=.1, decimals=1, label="Total speed limit (MB/s · 0 = unlimited)", width=ui.FILL)
            form["speed_limit"] = int(limit * 1_000_000)
            form["resume_on_start"] = ctx.switch("Pick up where you left off", form["resume_on_start"], description="Resume interrupted downloads when the daemon starts.")
            form["notifications"] = ctx.switch("A gentle heads-up", form["notifications"], description="Notify me when a download finishes.")
            if ctx.button("Save changes", icon=ui.Icon.CHECK, variant=ui.ButtonVariant.PRIMARY):
                self.patch({k: form[k] for k in ["download_dir", "music_dir", "max_concurrent", "connections", "speed_limit", "resume_on_start", "notifications"]})
                ctx.toast("Preferences saved", tone=ui.Tone.SUCCESS)
        with ctx.card(width=ui.FILL, pad=22, gap=17):
            ctx.heading("A color that feels like you.", level=3)
            ctx.secondary("Shared instantly with the browser companion.")
            modes = ["light", "dark", "system"]
            chosen = ctx.segmented("theme_mode", ["Light", "Dark", "System"], modes.index(state["settings"]["theme"]))
            if modes[chosen] != state["settings"]["theme"]:
                self.patch({"theme": modes[chosen]})
            self.custom_color = ctx.color_picker("accent_color", self.custom_color)
            color = f"#{self.custom_color.r:02x}{self.custom_color.g:02x}{self.custom_color.b:02x}"
            if color != state["settings"]["accent"]:
                self.patch({"accent": color})
            with ctx.row(gap=8, wrap=True):
                for accent in ACCENTS:
                    if ctx.button(f"  ##{accent}", color=accent, variant=ui.ButtonVariant.OUTLINE, tooltip=accent):
                        self.patch({"accent": accent})
                        self.custom_color = ui.Color.hex(accent)
            ctx.text("Current accent: " + state["settings"]["accent"], size=11, color=ui.Color.TEXT_SECONDARY)
        with ctx.card(width=ui.FILL, pad=22, gap=14):
            ctx.heading("A quiet engine. Always ready.", level=3)
            ctx.text(f"Daemon PID {state['daemon']['pid']} · Data: {state['daemon']['data_dir']}", size=11, color=ui.Color.TEXT_SECONDARY)
            ctx.text("Closing this window leaves the daemon running. To start at sign-in, run: ndm service install --start", size=11, color=ui.Color.TEXT_SECONDARY, selectable=True)
            running = ctx.switch("Background engine running", state["settings"]["engine_running"])
            if running != state["settings"]["engine_running"]:
                self.patch({"engine_running": running})

    def _dialogs(self, ctx: ui.UI, state: dict) -> None:
        with ctx.dialog("Good things, incoming.", self.add_open, description="Add a link. We’ll take care of the wait.", width=510) as dialog:
            if dialog:
                self.url = ctx.input("add_url", self.url, label="Download link", placeholder="https://example.com/file.zip", width=ui.FILL, max_bytes=8192)
                self.filename = ctx.input("add_name", self.filename, label="File name (optional)", width=ui.FILL)
                self.add_category = ctx.select("add_category", ["Detect automatically", "Video", "Audio", "Archive", "Image", "Document", "Other"], self.add_category, label="Category", width=ui.FILL)
                self.add_connections = int(ctx.number("add_connections", self.add_connections, 1, 8, label="Connections", width=ui.FILL))
                self.start_paused = ctx.checkbox("Add to queue without starting", self.start_paused)
                self.authorized = ctx.checkbox("I own this content or have permission to download it", self.authorized)
                if self.error:
                    ctx.alert(self.error[:250], tone=ui.Tone.DANGER)
                with ctx.dialog_footer():
                    if ctx.button("Cancel", variant=ui.ButtonVariant.GHOST):
                        dialog.open = False
                    if ctx.button("Start download", icon=ui.Icon.DOWNLOAD, variant=ui.ButtonVariant.PRIMARY):
                        self.add()
        self.add_open = dialog.open
        task = next((t for t in state["downloads"] if t["id"] == self.details_id), None)
        with ctx.dialog("Download details", bool(task), width=530) as detail_dialog:
            if detail_dialog and task:
                ctx.heading(task["name"], level=3)
                ctx.progress(task["downloaded"] / task["size"] * 100 if task["size"] else 0, show_value=True, width=ui.FILL)
                ctx.text(f"{format_bytes(task['downloaded'])} of {format_bytes(task['size'])} · {task['status']}", size=12)
                ctx.text("URL: " + task["url"], size=11, selectable=True, width=ui.FILL)
                ctx.text("Saved to: " + task["path"], size=11, selectable=True, width=ui.FILL)
                ctx.text(f"Connections: {task['connections']} · Resume: {'supported' if task['resumable'] else 'not detected'}", size=11)
                if task["error"]:
                    ctx.alert(task["error"], tone=ui.Tone.DANGER)
                with ctx.dialog_footer():
                    if ctx.button("Remove", icon=ui.Icon.TRASH, variant=ui.ButtonVariant.DANGER):
                        self.delete_id = task["id"]
                    if task["status"] == "completed":
                        if ctx.button("Show in folder", icon=ui.Icon.FOLDER):
                            try:
                                self.request("POST", "/open/" + task["id"])
                            except ValueError as exc:
                                self.error = str(exc)
                    elif ctx.button("Pause" if task["status"] in {"downloading", "queued"} else "Resume", variant=ui.ButtonVariant.PRIMARY):
                        self.action(task["id"], "pause" if task["status"] in {"downloading", "queued"} else "resume")
                    if ctx.button("Close", variant=ui.ButtonVariant.GHOST):
                        detail_dialog.open = False
        if not detail_dialog.open:
            self.details_id = None
        with ctx.dialog("Remove this download?", bool(self.delete_id), description="Unfinished partial files are deleted. Completed files stay on disk.", width=430) as confirm:
            if confirm:
                with ctx.dialog_footer():
                    if ctx.button("Keep it", variant=ui.ButtonVariant.GHOST):
                        confirm.open = False
                    if ctx.button("Remove", variant=ui.ButtonVariant.DANGER):
                        self.action(self.delete_id, "delete")
                        self.delete_id = None
                        self.details_id = None
                        confirm.open = False
        if not confirm.open:
            self.delete_id = None

    def run(self) -> None:
        settings = self.state["settings"]
        with ui.App("NDM — Your downloads, in flow", width=1320, height=860,
                    theme=ui.ThemeMode.DARK if settings["theme"] == "dark" else ui.ThemeMode.LIGHT) as app:
            self.app = app
            self.runner = ui.BackgroundRunner.for_app(app, max_workers=3)
            thread = threading.Thread(target=self._background, name="ndm-ui-state", daemon=True)
            thread.start()
            try:
                app.run(self.draw)
            finally:
                self.stop.set()
                self.runner.close()
                self.client.close()
                # Deliberately do not stop the daemon when the window closes.
