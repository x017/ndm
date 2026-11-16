"""Optional system tray supervisor; UI subprocesses never run as administrator."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path


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

    def quit_tray(icon, _item):
        icon.stop()

    icon = pystray.Icon("ndm", icon_image(), "NDM — Download Manager", menu=pystray.Menu(
        pystray.MenuItem("Open NDM (use task switcher if already open)", controller.open_desktop, default=True),
        pystray.MenuItem("Open Web UI (start ndm web separately)", controller.open_web),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit tray (keep downloads running)", quit_tray),
    ))

    def setup(icon):
        icon.visible = True
        if open_window:
            controller.open_desktop()

    try:
        icon.run(setup=setup)
    except Exception as exc:
        raise RuntimeError(
            "Could not start the system tray. Check your desktop's tray/AppIndicator "
            "support, or run ndm desktop without a tray."
        ) from exc
