import http.client
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from unittest import mock
import base64

from drive_relay import onboard, panel, routing
from drive_relay.config import load


class DesktopControls(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)/"profile"
        key = base64.b64encode(struct.pack(">I",11)+b"ssh-ed25519"+struct.pack(">I",32)+os.urandom(32)).decode()
        self.routes = dict(mode="auto", lan_ip="192.168.1.20", nas_name="nas.example.test",
                           drive_port=6690, verify_port=5001, cert_sha256="")
        self.invite = dict(schema="drive-ssh-relay/v2", host="relay.example.test",
            host_key="ssh-ed25519 "+key, ssh_port=443, bridge_port=16690, routing=self.routes)
        self.ui = panel.LocalPanel(self.state, 0)
        thread = threading.Thread(target=self.ui.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.ui.server_close)
        self.addCleanup(self.ui.shutdown)

    def request(self, action, body=None, auth=True):
        client=http.client.HTTPConnection("127.0.0.1",self.ui.server_port,timeout=4)
        headers={"Content-Type":"application/json","Origin":self.ui.origin}
        if auth:
            headers["Authorization"]="Bearer "+self.ui.key
        try:
            client.request("POST" if body is not None else "GET",action,
                           None if body is None else json.dumps(body),headers)
            r=client.getresponse()
            return r.status,r.read()
        finally:
            client.close()

    def test_page_enrols_and_updates_the_same_cli_profile(self):
        for name in ("/","/panel.js","/panel.css"):
            status, body=self.request(name)
            self.assertEqual(status,200)
            self.assertTrue(body)
        self.assertEqual(self.request("/api/status",{},False)[0],403)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1",0))
            local=sock.getsockname()[1]
        with mock.patch.object(onboard,"drive_client",return_value=None):
            code, body=self.request("/api/setup",{"invite":self.invite,"local_port":local})
            self.assertEqual(json.loads(body)["status"],"prerequisites")
            self.assertFalse(self.state.exists())
        with mock.patch.object(onboard,"drive_client",return_value="verified test fixture"):
            _, body=self.request("/api/setup",{"invite":self.invite,"local_port":local})
            key=json.loads(body)["device_public_key"]
            self.assertEqual(routing.settings(self.state).mode,"auto")
            original=(self.state/"identity").stat().st_mtime_ns
            code,body=self.request("/api/config",dict(self.routes,mode="relay"))
            self.assertEqual(code,200)
            self.assertEqual(json.loads(body)["routing"]["mode"],"relay")
            self.assertEqual(load(self.state).local_port,local)
            _,body=self.request("/api/setup",{"invite":self.invite,"local_port":local})
            self.assertEqual(json.loads(body)["device_public_key"],key)
            self.assertEqual((self.state/"identity").stat().st_mtime_ns, original)
            self.assertEqual(routing.settings(self.state).mode,"relay")
            self.assertEqual(self.request("/api/config",dict(self.routes,lan_ip="8.8.8.8"))[0],400)
            self.assertEqual(routing.settings(self.state).mode,"relay")
        _,status=self.request("/api/status",{})
        self.assertNotIn(b"PRIVATE KEY",status)

    def test_unknown_vps_input_or_unverified_lan_is_rejected(self):
        with self.assertRaises(ValueError):
            onboard.parse_invite(dict(self.invite, identity="unexpected"))
        for update in (dict(lan_ip="8.8.8.8"),dict(nas_name="",cert_sha256=""),
                       dict(cert_sha256="broken"),dict(verify_port=False)):
            with self.assertRaises(ValueError):
                routing.Routes(**dict(self.routes,**update))
