"""SQLite persistence. The daemon is the single writer/owner of a data directory."""
from __future__ import annotations

import os

if os.name == "nt":
    import msvcrt
else:
    import fcntl
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.lock = threading.RLock()
        self._lockfile = (directory / "daemon.lock").open("a+")
        try:
            if os.name == "nt":
                self._lockfile.seek(0)
                msvcrt.locking(self._lockfile.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self._lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            self._lockfile.close()
            raise RuntimeError("Another NDM daemon already owns this data directory.") from exc
        self.db = sqlite3.connect(directory / "ndm.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS downloads (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL,
                title TEXT NOT NULL, detail TEXT NOT NULL, tone TEXT NOT NULL
            );
        """)
        self.db.commit()

    def load_downloads(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            return {r["id"]: json.loads(r["payload"]) for r in self.db.execute("SELECT * FROM downloads")}

    def save_downloads(self, ids: list[str], tasks: dict[str, dict[str, Any]]) -> None:
        with self.lock:
            for task_id in ids:
                task = tasks.get(task_id)
                if task is None:
                    self.db.execute("DELETE FROM downloads WHERE id=?", (task_id,))
                else:
                    self.db.execute(
                        "INSERT OR REPLACE INTO downloads VALUES (?, ?)",
                        (task_id, json.dumps(task, separators=(",", ":"))),
                    )
            self.db.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))
            self.db.commit()

    def event(self, title: str, detail: str = "", tone: str = "info") -> None:
        with self.lock:
            self.db.execute("INSERT INTO events (time,title,detail,tone) VALUES (?,?,?,?)",
                            (time.time(), title, detail, tone))
            self.db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 200)")
            self.db.commit()

    def events(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 50")]

    def close(self) -> None:
        with self.lock:
            self.db.close()
            if os.name == "nt":
                self._lockfile.seek(0)
                msvcrt.locking(self._lockfile.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._lockfile, fcntl.LOCK_UN)
            self._lockfile.close()
