"""On-demand loopback control page. Session secrets stay in the URL fragment."""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import secrets
import subprocess
import threading
import webbrowser

from . import onboard, services
from .config import LOOPBACK, load
from .routing import Routes, save


class LocalPanel(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, state, port):
        self.state, self.key = state, secrets.token_urlsafe(32)
        self.change = threading.Lock()
        super().__init__((LOOPBACK, port), Handler)
        self.origin = "http://127.0.0.1:" + str(self.server_port)

    def dispatch(self, action, data):
        if action == "close":
            threading.Thread(target=self.shutdown, daemon=True).start()
            return {"closed": True}
        if action == "status":
            return onboard.describe(self.state)
        if action == "preflight":
            return onboard.preflight(self.state, data.get("local_port", 16690), data.get("drive_path"))
        with self.change:
            if action == "setup":
                return onboard.setup(self.state, data["invite"], data.get("local_port", 16690), data.get("drive_path"))
            if action == "config":
                save(self.state, load(self.state), Routes(**data))
            elif action in ("install", "stop", "start"):
                services.service(action, self.state, announce=False)
            else:
                raise ValueError("unknown operation")
        return onboard.describe(self.state)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, data, media="application/json"):
        if media == "application/json":
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", media + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def host_ok(self):
        return self.headers.get("Host") == self.server.origin.split("//", 1)[1]

    def do_GET(self):
        assets = {"/": ("panel.html", "text/html"), "/panel.js": ("panel.js", "text/javascript"), "/panel.css": ("panel.css", "text/css")}
        if not self.host_ok() or self.path not in assets:
            self.reply(404, {"error": "not found"})
            return
        name, media = assets[self.path]
        self.reply(200, files("drive_relay").joinpath(name).read_bytes(), media)

    def do_POST(self):
        if (not self.host_ok() or self.headers.get("Origin") != self.server.origin or
                not hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + self.server.key).encode())):
            self.reply(403, {"error": "open the private panel URL from the launcher"})
            return
        if not self.path.startswith("/api/") or self.headers.get_content_type() != "application/json":
            self.reply(400, {"error": "expected JSON"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if not 0 <= length <= 24576:
                raise ValueError("request too large")
            self.connection.settimeout(5)
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            result = self.server.dispatch(self.path[5:], data)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as e:
            self.reply(400, {"error": str(e)})
            return
        self.reply(200, result)


def run(state, port, open_browser):
    with LocalPanel(state, port) as panel:
        url = panel.origin + "/#" + panel.key
        print(url, flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            panel.serve_forever()
        except KeyboardInterrupt:
            pass
