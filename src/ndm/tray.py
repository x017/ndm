"""Optional system tray supervisor; UI subprocesses never run as administrator."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from .daemon import is_running, start_detached
from .ipc import request

log = logging.getLogger("ndm.tray")


def icon_image():
    from PIL import Image, ImageDraw
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 62, 62), radius=15, fill="#7856e8")
    draw.line((32, 13, 32, 42), fill="white", width=5)
    draw.line((20, 31, 32, 43, 44, 31), fill="white", width=5)
    draw.line((17, 51, 47, 51), fill="white", width=4)
    return image


class TrayController:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.desktop = None
        self.lock = threading.Lock()

    def ensure_daemon(self):
        if is_running(self.data_dir):
            return
        start_detached(self.data_dir)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if is_running(self.data_dir):
                return
            time.sleep(0.1)
        raise RuntimeError("NDM daemon did not start. Check ndm logs and the data directory's daemon.log.")

    def set_running(self, running):
        self.ensure_daemon()
        request(self.data_dir, "settings", {"engine_running": running}, timeout=5)

    def status(self):
        state = request(self.data_dir, "state", timeout=5)
        stats = state["stats"]
        mode = "Running" if state["settings"]["engine_running"] else "Paused"
        return f"NDM — {mode}: {stats['active']} active, {stats['queued']} queued"

    def open_desktop(self, *_):
        with self.lock:
            if self.desktop is None or self.desktop.poll() is not None:
                self.desktop = subprocess.Popen([
                    os.path.abspath(sys.executable), "-m", "ndm", "desktop",
                    "--data-dir", str(self.data_dir),
                ])
        # Nexa has no portable window-activation API. An existing window stays
        # open rather than spawning duplicates; use the OS task switcher.

    def open_web(self, *_):
        # Never silently start a public listener or invent web credentials.
        webbrowser.open("http://127.0.0.1:8765")


def run(data_dir: Path, *, open_window=True):
    try:
        import pystray
        from PIL import Image  # noqa: F401
    except Exception as exc:
        raise RuntimeError(
            "System tray unavailable. Install NDM with the tray extra and run in a "
            "graphical session: uv sync --extra tray; uv run ndm tray. "
            "Linux also needs a working tray/AppIndicator host."
        ) from exc
    controller = TrayController(data_dir)

    stopped = threading.Event()

    def guarded(action):
        def callback(icon, _item):
            def work():
                try:
                    action()
                except Exception as exc:
                    log.exception("Tray action failed")
                    try:
                        icon.notify(str(exc), "NDM")
                    except Exception:
                        pass
            threading.Thread(target=work, name="ndm-tray-action", daemon=True).start()
        return callback

    def quit_tray(icon, _item):
        stopped.set()
        icon.stop()

    icon = pystray.Icon("ndm", icon_image(), "NDM — Download Manager", menu=pystray.Menu(
        pystray.MenuItem("Open NDM (use task switcher if already open)", guarded(controller.open_desktop), default=True),
        pystray.MenuItem("Open Web UI (start ndm web separately)", guarded(controller.open_web)),
        pystray.MenuItem("Pause all downloads", guarded(lambda: controller.set_running(False))),
        pystray.MenuItem("Resume download engine", guarded(lambda: controller.set_running(True))),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit tray (keep downloads running)", quit_tray),
    ))

    def setup(icon):
        icon.visible = True
        def monitor():
            try:
                controller.ensure_daemon()
                if open_window:
                    controller.open_desktop()
            except Exception:
                log.exception("Tray startup failed")
            while not stopped.is_set():
                try:
                    icon.title = controller.status()
                except Exception:
                    icon.title = "NDM — daemon unavailable (see ndm logs)"
                stopped.wait(3)
        threading.Thread(target=monitor, name="ndm-tray-status", daemon=True).start()

    try:
        icon.run(setup=setup)
    except Exception as exc:
        raise RuntimeError(
            "Could not start the system tray. Check your desktop's tray/AppIndicator "
            "support, or run ndm desktop without a tray."
        ) from exc
    finally:
        stopped.set()
