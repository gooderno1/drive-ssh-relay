"""Loopback management service. A TLS reverse proxy serves the configured origin."""

import argparse
from dataclasses import asdict
import getpass
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
from . import __version__, onboard
from .fleetdb import Store, Denied
from .routing import Routes

COOKIE = "relay_admin"


def origin(value, allow_local=False):
    v = urlsplit(value)
    if (
        not v.hostname
        or v.username is not None
        or v.password is not None
        or v.path not in ("", "/")
        or v.query
        or v.fragment
    ):
        raise ValueError("请输入完整的 HTTPS 管理台地址")
    if v.scheme != "https" and not (
        allow_local and v.scheme == "http" and v.hostname in ("localhost", "127.0.0.1")
    ):
        raise ValueError("管理台需要 HTTPS")
    if v.port is not None and not 1 <= v.port <= 65535:
        raise ValueError("invalid port")
    return v.scheme + "://" + v.netloc


class Fleet(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, folder, url, port=0, allow_local=False):
        self.folder = folder
        self.origin = origin(url, allow_local)
        self.store = Store(folder / "fleet.sqlite3")
        self.store.prepare()
        self.invitation = onboard.read_invite(folder / "invite.json")
        self.routes = onboard.parse_invite(self.invitation)[1]
        self.package = folder / "downloads" / "drive-ssh-relay.zip"
        self.secure = self.origin.startswith("https://")
        super().__init__(("127.0.0.1", port), Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "DriveRelay"

    def setup(self):
        super().setup()
        self.connection.settimeout(12)

    def log_message(self, *args):
        pass

    def reply(self, code, body, kind="application/json", headers=()):
        if kind == "application/json":
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def host_ok(self):
        return self.headers.get("Host") == urlsplit(self.server.origin).netloc

    def session(self):
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except CookieError:
            return ""
        return jar[COOKIE].value if COOKIE in jar else ""

    def cookie(self, token, age):
        return (
            "Set-Cookie",
            f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}"
            + ("; Secure" if self.server.secure else ""),
        )

    def do_GET(self):
        if not self.host_ok():
            return self.reply(404, {})
        store = self.server.store
        try:
            assets = {
                "/": ("fleet.html", "text/html; charset=utf-8"),
                "/fleet.js": ("fleet.js", "text/javascript"),
                "/fleet.css": ("fleet.css", "text/css; charset=utf-8"),
                "/panel.css": ("panel.css", "text/css"),
            }
            if self.path in assets:
                file, mime = assets[self.path]
                return self.reply(200, files("drive_relay").joinpath(file).read_bytes(), mime)
            if self.path == "/healthz":
                return self.reply(200, {"up": True})
            store.admin(self.session())
            if self.path == "/api/admin/overview":
                return self.reply(
                    200,
                    dict(
                        devices=store.devices(),
                        version=__version__,
                        download=self.server.package.is_file(),
                        origin=self.server.origin,
                        default_policy=asdict(self.server.routes),
                    ),
                )
            if self.path == "/download/client.zip" and self.server.package.is_file():
                return self.reply(
                    200,
                    self.server.package.read_bytes(),
                    "application/zip",
                    [
                        (
                            "Content-Disposition",
                            'attachment; filename="Drive-SSH-Relay-' + __version__ + '.zip"',
                        )
                    ],
                )
            self.reply(404, {})
        except Denied:
            self.reply(401, {"error": "请登录管理台"})

    def do_POST(self):
        if not self.host_ok():
            return self.reply(404, {})
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if self.headers.get_content_type() != "application/json" or not 0 <= length <= 24576:
                return self.reply(400, {"error": "invalid JSON request"})
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("invalid input")
            store = self.server.store
            if self.path == "/api/device/preview":
                store.valid_pairing(data["code"])
                return self.reply(200, self.server.invitation)
            if self.path == "/api/device/pair":
                return self.reply(
                    200, store.pair(data["code"], data["public_key"], data["token"], self.server.routes)
                )
            if self.path == "/api/device/checkin":
                head = self.headers.get("Authorization", "")
                if not head.startswith("Bearer "):
                    raise Denied()
                return self.reply(200, store.checkin(head[7:], data))
            if self.headers.get("Origin") != self.server.origin:
                return self.reply(403, {"error": "invalid origin"})
            if self.path == "/api/admin/activate":
                store.activate(data.get("code"), data.get("password"))
                return self.reply(200, {"ok": True})
            if self.path == "/api/admin/login":
                # The dedicated reverse proxy overwrites X-Real-IP, or leaves it absent.
                peer = self.headers.get("X-Real-IP", self.client_address[0])[:64]
                token = store.login(data.get("password"), peer)
                return self.reply(200, {"ok": True}, headers=[self.cookie(token, 28800)])
            token = self.session()
            store.admin(token)
            if self.path == "/api/admin/logout":
                store.logout(token)
                return self.reply(200, {"ok": True}, headers=[self.cookie("", 0)])
            if self.path == "/api/admin/invite":
                return self.reply(200, store.invite(data["name"]))
            if self.path == "/api/admin/policy":
                return self.reply(200, store.policy(data["id"], data["revision"], data["policy"]))
            if self.path == "/api/admin/disable":
                return self.reply(200, store.disable(data["id"]))
            if self.path == "/api/admin/grant":
                return self.reply(200, store.grant(data["id"], data["allow"]))
            self.reply(404, {})
        except Denied:
            self.reply(401, {"error": "登录信息、配对码或设备凭证无效"})
        except (ValueError, TypeError, KeyError, UnicodeError) as e:
            self.reply(400, {"error": str(e)})
        except OSError:
            self.reply(503, {"error": "服务暂时不可用"})


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["serve", "password", "issue-setup"])
    p.add_argument("--data", required=True, type=Path)
    p.add_argument("--origin")
    p.add_argument("--port", type=int, default=18441)
    p.add_argument("--stdin", action="store_true", help="read a password from a trusted pipe")
    args = p.parse_args(argv)
    args.data.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.umask(0o077)
    if args.command == "issue-setup":
        store = Store(args.data / "fleet.sqlite3")
        store.prepare()
        store.issue_setup(sys.stdin.readline().strip())
        return
    if args.command == "password":
        store = Store(args.data / "fleet.sqlite3")
        store.prepare()
        pwd = (
            sys.stdin.readline().rstrip("\r\n")
            if args.stdin
            else getpass.getpass("New administrator password: ")
        )
        if not args.stdin and pwd != getpass.getpass("Repeat: "):
            raise SystemExit("Password mismatch")
        store.password(pwd)
        print("Password updated. Existing sessions signed out.")
        return
    if not args.origin:
        p.error("--origin is required")
    with Fleet(args.data, args.origin, args.port, allow_local=True) as http:
        http.serve_forever()


if __name__ == "__main__":
    main()
