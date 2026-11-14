import base64
from dataclasses import asdict
import http.client
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest import mock
from drive_relay import fleet, managed, observed, onboard, routing
from drive_relay.config import Profile
from drive_relay.fleetdb import Denied, secret_hash


def key():
    return (
        "ssh-ed25519 "
        + base64.b64encode(
            struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + os.urandom(32)
        ).decode()
    )


class Management(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / "desktop"
        self.profile = key()
        self.base = "https://fleet.example.test:8443"
        self.inv = {
            "schema": "drive-ssh-relay/v2",
            "host": "192.0.2.8",
            "ssh_port": 443,
            "bridge_port": 16690,
            "host_key": self.profile,
            "routing": asdict(routing.Routes()),
        }
        (self.root / "invite.json").write_text(json.dumps(self.inv))
        self.server = fleet.Fleet(self.root, self.base)
        self.db = self.server.store
        self.db.password("initial-test-password7")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def http(self, path, body=None, cookie="", origin=None, bearer=None):
        c = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        hdr = {
            "Host": "fleet.example.test:8443",
            "Origin": self.base if origin is None else origin,
            "Cookie": cookie,
            "Content-Type": "application/json",
        }
        if bearer:
            hdr["Authorization"] = "Bearer " + bearer
        try:
            c.request(
                "GET" if body is None else "POST", path, None if body is None else json.dumps(body), hdr
            )
            r = c.getresponse()
            data = r.read()
            return r.status, data, r.getheader("Set-Cookie")
        finally:
            c.close()

    def test_initial_password_link_is_single_use_and_ends_prior_sessions(self):
        previous = self.db.login("initial-test-password7", "loopback")
        code = "b" * 43
        self.db.issue_setup(code)
        self.db.activate(code, "aaaaaaa1")
        with self.assertRaises(Denied):
            self.db.admin(previous)
        with self.assertRaises(Denied):
            self.db.activate(code, "other-password2")
        self.db.admin(self.db.login("aaaaaaa1", "loopback"))
        self.db.issue_setup(code)
        self.db.password("replacement-local-test-password2")
        with self.assertRaises(Denied):
            self.db.activate(code, "other-password2")

    def test_web_setup_and_operator_reset_require_letters_and_digits(self):
        code = "p" * 43
        self.db.issue_setup(code)
        token = self.db.login("initial-test-password7", "local")
        for invalid in ("abc1234", "abcdefgh", "12345678", "abcd!@#$", "1234!@#$", "a" * 256 + "1", None):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.db.password(invalid)
                status, _, _ = self.http("/api/admin/activate", {"code": code, "password": invalid})
                self.assertEqual(status, 400)
        self.db.admin(token)
        for password in ("abcde123", "é!💡123ab", "a" * 255 + "１"):
            with self.subTest(length=len(password)):
                self.assertEqual(self.http("/api/admin/activate", {"code": code, "password": password})[0], 200)
                self.assertEqual(self.http("/api/admin/login", {"password": password})[0], 200)
                self.db.password(password)
                self.db.admin(self.db.login(password, "local"))
                self.db.issue_setup(code)

    def test_legacy_password_still_logs_in(self):
        # Existing versions allowed a passphrase without a digit.
        salt = os.urandom(16)
        previous = "previous-passphrase"
        with self.db.db(True) as c:
            c.execute("UPDATE admin SET salt=?,verifier=? WHERE id=1", (salt, secret_hash(previous, salt)))
        self.assertEqual(self.http("/api/admin/login", {"password": previous})[0], 200)

    def test_access_boundaries_and_pair_retries(self):
        self.assertEqual(self.http("/download/client.zip")[0], 401)
        self.assertEqual(self.http("/api/admin/overview")[0], 401)
        self.assertEqual(self.http("/api/admin/login", {"password": "incorrect"})[0], 401)
        status, _, cookie = self.http("/api/admin/login", {"password": "initial-test-password7"})
        self.assertEqual(status, 200)
        for attr in ("HttpOnly", "Secure", "SameSite=Strict"):
            self.assertIn(attr, cookie)
        c = cookie.split(";")[0]
        self.assertEqual(
            self.http("/api/admin/invite", {"name": "office"}, c, origin="https://other.example.test")[
                0
            ],
            403,
        )
        code = json.loads(self.http("/api/admin/invite", {"name": "office"}, c)[1])["code"]
        pair = {"code": code, "public_key": key(), "token": "x" * 43}
        a = self.http("/api/device/pair", pair)
        self.assertEqual(a[0], 200)
        self.assertEqual(self.http("/api/device/pair", pair)[1], a[1])
        self.assertEqual(self.http("/api/device/pair", dict(pair, token="y" * 43))[0], 401)
        dev = json.loads(a[1])["id"]
        self.assertEqual(self.http("/api/admin/overview", bearer=pair["token"])[0], 401)
        policy = {"id": dev, "revision": 1, "policy": asdict(routing.Routes())}
        self.assertEqual(self.http("/api/admin/policy", policy, c)[0], 200)
        self.assertEqual(self.http("/api/admin/policy", policy, c)[0], 400)
        self.assertEqual(self.http("/api/admin/grant", {"id": dev, "allow": True}, c)[0], 200)
        self.assertEqual(self.http("/api/admin/disable", {"id": dev}, c)[0], 200)
        self.assertFalse(self.db.devices()[0]["ssh_granted"])
        self.assertEqual(self.http("/api/device/checkin", {"status": {}}, bearer=pair["token"])[0], 401)
        self.http("/api/admin/logout", {}, c)
        self.assertEqual(self.http("/api/admin/overview", cookie=c)[0], 401)

    def test_desktop_applies_and_acknowledges_selected_device_policy(self):
        store = self.db
        code = store.invite("laptop")["code"]

        def transport(url, action, data, token=None):
            if action == "preview":
                store.valid_pairing(data["code"])
                return self.inv
            if action == "pair":
                return store.pair(data["code"], data["public_key"], data["token"], routing.Routes())
            return store.checkin(token, data)

        with (
            mock.patch.object(managed, "request", side_effect=transport),
            mock.patch.object(onboard, "drive_client", return_value="installed"),
            mock.patch.object(onboard.runtime, "local_free", return_value=True),
        ):
            managed.join(self.state, self.base, code, 39401)
            managed.sync(self.state)
            device = store.devices()[0]
            self.assertEqual(device["ack"], 1)
            new = routing.Routes("auto", "192.168.10.2", "nas.example.test")
            store.policy(device["id"], 1, asdict(new))
            managed.sync(self.state)
            self.assertEqual(routing.settings(self.state), new)
            self.assertEqual(store.devices()[0]["ack"], 2)
        public = onboard.describe(self.state)
        self.assertNotIn("token", public["fleet"])

    def test_external_owner_is_distinguished_from_unrelated_listener(self):
        p = Profile("client", "192.0.2.8", self.profile)
        rt = {"phase": "inactive"}
        rows = [
            (2, "127.0.0.1", 16690, "0.0.0.0", 0, 20),
            (5, "192.0.2.3", 4451, p.host, 443, 20),
            (5, "127.0.0.1", 3005, "127.0.0.1", 16690, 21),
        ]
        ssh = str(Path(os.environ.get("WINDIR", "C:/Windows")) / "System32/OpenSSH/ssh.exe")
        look = lambda pid: ssh if pid == 20 else "cloud-drive-daemon.exe"
        self.assertEqual(
            observed.classify(rows, p, rt, look, True),
            {"kind": "legacy", "route": "vps", "connections": 1},
        )
        self.assertEqual(observed.classify(rows[:1], p, rt, look, True)["kind"], "other")
        self.assertEqual(observed.classify(rows, p, rt, look, False)["kind"], "other")
