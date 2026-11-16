"""Optional browser frontend. The download engine remains in the native daemon."""
from __future__ import annotations

import json
import os
import secrets
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..ipc import request
from .views import dialog, page, queue

STATIC = Path(__file__).parent / "static"


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, data_dir: Path, *, token=None, username=None, password=None):
        self.data_dir = data_dir
        self.token = token or secrets.token_urlsafe(32)
        self.username = username or os.environ.get("NDM_WEB_USERNAME", "admin")
        self.password = password or os.environ.get("NDM_WEB_PASSWORD") or secrets.token_urlsafe(16)
        self.login_attempts = {}
        self.csrf = secrets.token_urlsafe(32)
        super().__init__(address, Handler)

    def call(self, operation, payload=None):
        return request(self.data_dir, operation, payload, timeout=300)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Never log query strings: the initial login URL contains a secret.
        pass

    def send(self, content, status=200, *, mime="text/html; charset=utf-8", headers=None):
        body = content.encode() if isinstance(content, str) else content
        self.send_response(status)
        for key, value in {
            "Content-Type": mime, "Content-Length": str(len(body)),
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            **(headers or {}),
        }.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            value = cookie.get("ndm_session")
            return value is not None and secrets.compare_digest(value.value, self.server.token)
        except Exception:
            return False

    def login_page(self, error=""):
        from html import escape
        return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in · NDM</title><link rel="stylesheet" href="/static/app.css"></head><body><main style="min-height:100dvh;align-items:center;justify-content:center"><section class="dialog" style="width:400px"><h2>↧ NDM</h2><p class="muted">Sign in to your download manager.</p><form method="post" action="/login"><label>Username<input name="username" autocomplete="username" required autofocus></label><label>Password<input name="password" type="password" autocomplete="current-password" required></label><p class="error" role="alert">' + escape(error) + '</p><button class="primary" style="width:100%">Sign in</button></form></section></main></body></html>'

    def do_GET(self):
        route = urlsplit(self.path)
        params = parse_qs(route.query)
        if not self.authorized() and not route.path.startswith("/static/"):
            return self.send(self.login_page())
        try:
            if route.path.startswith("/static/"):
                name = route.path.removeprefix("/static/")
                if name not in {"htmx.min.js", "alpine.min.js", "app.css"}:
                    return self.send("Not found", 404)
                return self.send((STATIC / name).read_bytes(), mime="text/css" if name.endswith("css") else "text/javascript")
            if route.path == "/":
                return self.send(page(self.server.csrf))
            if route.path == "/queue":
                return self.send(queue(self.server.call("state"), params))
            if route.path == "/dialog":
                return self.send(dialog(params.get("kind", ["add"])[0], self.server.call("state")))
            return self.send("Not found", 404)
        except Exception as exc:
            from html import escape
            self.send(f'<div class="notice error">{escape(str(exc))}</div>', 200)

    def do_POST(self):
        if urlsplit(self.path).path == "/login":
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                return self.send("Forbidden origin", 403)
            now = time.monotonic()
            attempts = [t for t in self.server.login_attempts.get(self.client_address[0], []) if now - t < 60]
            self.server.login_attempts[self.client_address[0]] = attempts
            if len(attempts) >= 10:
                return self.send(self.login_page("Too many attempts. Try again in a minute."), 429)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    return self.send("Invalid request", 400)
                values = parse_qs(self.rfile.read(length).decode())
                valid = secrets.compare_digest(values.get("username", [""])[0].encode(), self.server.username.encode())
                valid &= secrets.compare_digest(values.get("password", [""])[0].encode(), self.server.password.encode())
            except (ValueError, UnicodeError):
                valid = False
            if not valid:
                attempts.append(now)
                return self.send(self.login_page("Incorrect username or password."), 401)
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            return self.send("", 303, headers={"Location": "/", "Set-Cookie": f"ndm_session={self.server.token}; HttpOnly; SameSite=Strict; Path=/{secure}"})
        if not self.authorized() or not secrets.compare_digest(self.headers.get("X-NDM-CSRF", ""), self.server.csrf):
            return self.send("Forbidden", 403)
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            return self.send("Forbidden origin", 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 65536:
                return self.send("Request too large", 413)
            data = {k: v[-1] for k, v in parse_qs(self.rfile.read(length).decode(), keep_blank_values=True).items()}
            route = urlsplit(self.path).path
            if route == "/add":
                self.server.call("add", {"url": data["url"], "filename": data.get("filename") or None, "start_paused": data.get("paused") == "on"})
            elif route == "/action":
                self.server.call("action", {"id": data["id"], "action": data["action"]})
            elif route == "/engine":
                self.server.call("settings", {"engine_running": data["running"] == "true"})
            elif route == "/settings":
                from ..models import Settings
                fields = Settings.model_fields
                payload = {}
                for key, value in data.items():
                    if key not in fields:
                        raise ValueError("Unknown setting")
                    annotation = fields[key].annotation
                    payload[key] = value == "true" if annotation is bool else int(value) if annotation is int else value
                self.server.call("settings", payload)
            elif route == "/resolve":
                self.server.call("browser.resolve", {"id": data["id"], "accept": data["accept"] == "true"})
            elif route == "/grab":
                from ..media import grab_page_kind
                kind = grab_page_kind(data["url"])
                if kind == "spotify":
                    result = self.server.call("spotify.search", {"url": data["url"]})
                    songs = [item["song"] for item in result["items"]]
                    self.server.call("spotify.queue", {"songs": songs})
                else:
                    self.server.call("media.download", {"url": data["url"], "quality": data.get("quality", "best")})
            else:
                return self.send("Not found", 404)
            self.send('<div class="notice">Saved to NDM.</div>', headers={"HX-Trigger": json.dumps({"queueChanged": True, "close-dialog": True})})
        except Exception as exc:
            from html import escape
            self.send(f'<div class="notice error">{escape(str(exc))}</div>')


def run(data_dir: Path, host="127.0.0.1", port=8765):
    from ..daemon import is_running, start_detached
    if not is_running(data_dir):
        start_detached(data_dir)
    server = WebServer((host, port), data_dir)
    print(f"NDM Web: http://{host}:{server.server_port}/", flush=True)
    print(f"Username: {server.username}", flush=True)
    if not os.environ.get("NDM_WEB_PASSWORD"):
        print(f"Generated password: {server.password}", flush=True)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print("Network access enabled. Use a trusted network or an HTTPS reverse proxy; keep your credentials private.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
