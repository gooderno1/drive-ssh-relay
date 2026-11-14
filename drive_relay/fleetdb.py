"""Storage for administrator sessions, single-use pairing and per-device routes."""

import base64
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from .config import public_key
from .routing import Routes


def digest(text):
    if not isinstance(text, str) or len(text) > 256:
        return ""
    return hashlib.sha256(text.encode()).hexdigest()


def secret_hash(value, salt):
    return hashlib.scrypt(
        value.encode(), salt=salt, n=2**15, r=8, p=1, dklen=32, maxmem=96 * 1024 * 1024
    )


def name(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value.strip()) <= 64
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("设备名称需为 1 至 64 个可见字符")
    return value.strip()


class Denied(Exception):
    pass


class Store:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def db(self, write=False):
        con = sqlite3.connect(self.path, timeout=8, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            if write:
                con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        finally:
            con.close()

    def prepare(self):
        with self.db() as c:
            c.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS admin(id INTEGER PRIMARY KEY, salt BLOB NOT NULL, verifier BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS pairings(code TEXT PRIMARY KEY, name TEXT NOT NULL, expires INTEGER NOT NULL, device TEXT);
                CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,name TEXT NOT NULL,token TEXT UNIQUE NOT NULL,
                  pub TEXT UNIQUE NOT NULL, policy TEXT NOT NULL,rev INTEGER NOT NULL DEFAULT 1,ack INTEGER NOT NULL DEFAULT 0,
                  seen INTEGER,status TEXT NOT NULL DEFAULT '{}',disabled INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS attempts(bucket TEXT PRIMARY KEY,n INTEGER NOT NULL,until INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS bootstrap(id INTEGER PRIMARY KEY, token TEXT NOT NULL,expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS ssh_grants(device TEXT PRIMARY KEY REFERENCES devices(id));
            """)

    def password(self, value):
        if not isinstance(value, str) or not 14 <= len(value) <= 256:
            raise ValueError("管理密码须为 14 至 256 个字符")
        salt = secrets.token_bytes(16)
        proof = secret_hash(value, salt)
        with self.db(True) as c:
            c.execute("INSERT OR REPLACE INTO admin VALUES(1,?,?)", (salt, proof))
            c.execute("DELETE FROM sessions")
            c.execute("DELETE FROM bootstrap")

    def issue_setup(self, code):
        if not re.fullmatch(r"[A-Za-z0-9_-]{43,86}", code):
            raise ValueError("invalid setup code")
        with self.db(True) as c:
            c.execute(
                "INSERT OR REPLACE INTO bootstrap VALUES(1,?,?)",
                (digest(code), int(time.time()) + 86400),
            )

    def activate(self, code, password):
        with self.db() as c:
            row = c.execute(
                "SELECT token FROM bootstrap WHERE id=1 AND expires>?", (int(time.time()),)
            ).fetchone()
        if not row or not hmac.compare_digest(row[0], digest(code)):
            raise Denied()
        if not isinstance(password, str) or not 14 <= len(password) <= 256:
            raise ValueError("密码须为 14 至 256 个字符")
        salt = secrets.token_bytes(16)
        proof = secret_hash(password, salt)
        with self.db(True) as c:
            if not c.execute(
                "DELETE FROM bootstrap WHERE id=1 AND token=? AND expires>?",
                (digest(code), int(time.time())),
            ).rowcount:
                raise Denied()
            c.execute("INSERT OR REPLACE INTO admin VALUES(1,?,?)", (salt, proof))
            c.execute("DELETE FROM sessions")

    def login(self, value, peer):
        if not isinstance(value, str) or not 1 <= len(value) <= 256:
            raise Denied()
        now = int(time.time())
        with self.db(True) as c:
            c.execute("DELETE FROM attempts WHERE until<=?", (now,))
            for bucket, cap in (("client:" + peer, 12), ("all", 100)):
                row = c.execute("SELECT n FROM attempts WHERE bucket=?", (bucket,)).fetchone()
                if row and row["n"] >= cap:
                    raise Denied()
            for bucket in ("client:" + peer, "all"):
                c.execute(
                    "INSERT INTO attempts VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET n=n+1",
                    (bucket, now + 300),
                )
            row = c.execute("SELECT salt,verifier FROM admin WHERE id=1").fetchone()
        if not row or not hmac.compare_digest(row["verifier"], secret_hash(value, row["salt"])):
            raise Denied()
        token = secrets.token_urlsafe(32)
        with self.db(True) as c:
            current = c.execute("SELECT verifier FROM admin WHERE id=1").fetchone()
            if not current or not hmac.compare_digest(current[0], row["verifier"]):
                raise Denied()
            c.execute("DELETE FROM sessions WHERE expires<=?", (now,))
            c.execute("DELETE FROM attempts WHERE bucket=?", ("client:" + peer,))
            c.execute("INSERT INTO sessions VALUES(?,?)", (digest(token), now + 8 * 3600))
        return token

    def admin(self, token):
        with self.db() as c:
            if not c.execute(
                "SELECT 1 FROM sessions WHERE token=? AND expires>?", (digest(token), int(time.time()))
            ).fetchone():
                raise Denied()

    def logout(self, token):
        with self.db(True) as c:
            c.execute("DELETE FROM sessions WHERE token=?", (digest(token),))

    def invite(self, label):
        code = secrets.token_urlsafe(24)
        now = int(time.time())
        with self.db(True) as c:
            c.execute("DELETE FROM pairings WHERE expires<=?", (now,))
            c.execute(
                "INSERT INTO pairings(code,name,expires) VALUES(?,?,?)",
                (digest(code), name(label), now + 900),
            )
        return {"code": code, "expires": now + 900}

    def valid_pairing(self, code):
        with self.db() as c:
            if not c.execute(
                "SELECT 1 FROM pairings WHERE code=? AND expires>?", (digest(code), int(time.time()))
            ).fetchone():
                raise Denied()

    def pair(self, code, pub, token, routes):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43,86}", token):
            raise Denied()
        canonical = public_key(pub)
        now = int(time.time())
        dev = secrets.token_hex(12)
        with self.db(True) as c:
            row = c.execute(
                "SELECT name,device FROM pairings WHERE code=? AND expires>?", (digest(code), now)
            ).fetchone()
            if not row:
                raise Denied()
            if row["device"]:
                if not c.execute(
                    "SELECT 1 FROM devices WHERE id=? AND pub=? AND token=? AND disabled=0",
                    (row["device"], canonical, digest(token)),
                ).fetchone():
                    raise Denied()
                return {"id": row["device"], "name": row["name"]}
            try:
                c.execute(
                    "INSERT INTO devices(id,name,token,pub,policy) VALUES(?,?,?,?,?)",
                    (dev, row[0], digest(token), canonical, json.dumps(asdict(routes))),
                )
            except sqlite3.IntegrityError:
                raise ValueError("此设备身份已经登记")
            c.execute("UPDATE pairings SET device=? WHERE code=?", (dev, digest(code)))
        return {"id": dev, "name": row[0]}

    def checkin(self, token, data):
        status = data.get("status")
        ack = data.get("ack", 0)
        if not isinstance(status, dict) or type(ack) is not int or ack < 0:
            raise ValueError("invalid device report")

        def enum(key, choices, default):
            val = status.get(key, default)
            if val not in choices:
                raise ValueError("invalid " + key)
            return val

        count = status.get("connections")
        if count is not None and (type(count) is not int or not 0 <= count <= 65535):
            raise ValueError("invalid connection count")
        version = str(status.get("version", ""))[:32]
        report = dict(
            kind=enum("kind", ("managed", "legacy", "idle", "other", "unknown"), "unknown"),
            route=enum("route", ("lan", "vps", None), None),
            platform=enum("platform", ("win32", "darwin", "linux"), "linux"),
            phase=enum(
                "phase",
                (
                    "routing",
                    "ssh_running",
                    "inactive",
                    "retrying",
                    "blocked",
                    "stopped",
                    "starting",
                    "unknown",
                ),
                "unknown",
            ),
            connections=count,
            version=version,
        )
        with self.db(True) as c:
            row = c.execute(
                "SELECT * FROM devices WHERE token=? AND disabled=0", (digest(token),)
            ).fetchone()
            if not row:
                raise Denied()
            if ack > row["rev"]:
                raise ValueError("unknown revision")
            c.execute(
                "UPDATE devices SET seen=?,status=?,ack=? WHERE id=?",
                (int(time.time()), json.dumps(report), ack, row["id"]),
            )
            return {"revision": row["rev"], "policy": json.loads(row["policy"])}

    def devices(self):
        with self.db() as c:
            rows = c.execute(
                "SELECT id,name,pub,policy,rev,ack,seen,status,disabled, EXISTS(SELECT 1 FROM ssh_grants g WHERE g.device=id) AS ssh_granted FROM devices ORDER BY name,id"
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["policy"] = json.loads(item["policy"])
            item["status"] = json.loads(item["status"])
            item["online"] = bool(
                item["seen"] and item["seen"] > time.time() - 150 and not item["disabled"]
            )
            items.append(item)
        return items

    def policy(self, dev, revision, value):
        routes = Routes(**value)
        if type(revision) is not int:
            raise ValueError("invalid revision")
        with self.db(True) as c:
            row = c.execute(
                "UPDATE devices SET policy=?,rev=rev+1 WHERE id=? AND rev=? AND disabled=0",
                (json.dumps(asdict(routes)), dev, revision),
            )
            if not row.rowcount:
                raise ValueError("设备配置已更新或登记已停用，请刷新")
        return {"saved": True, "revision": revision + 1}

    def disable(self, dev):
        with self.db(True) as c:
            if not c.execute("UPDATE devices SET disabled=1 WHERE id=?", (dev,)).rowcount:
                raise ValueError("设备不存在")
            c.execute("DELETE FROM ssh_grants WHERE device=?", (dev,))
        return {"disabled": True}

    def grant(self, dev, allow):
        if type(allow) is not bool:
            raise ValueError("invalid authorization")
        with self.db(True) as c:
            if not c.execute("SELECT 1 FROM devices WHERE id=? AND disabled=0", (dev,)).fetchone():
                raise ValueError("设备已停用或不存在")
            if allow:
                c.execute("INSERT OR IGNORE INTO ssh_grants VALUES(?)", (dev,))
            else:
                c.execute("DELETE FROM ssh_grants WHERE device=?", (dev,))
        return {"ssh_granted": allow}
