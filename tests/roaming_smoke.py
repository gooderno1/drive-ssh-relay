"""Real desktop supervisor and OpenSSH, two isolated paths and changing NAS identity."""
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time

import paramiko
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from drive_relay import config, onboard


def certificate(path, label):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "nas.example.test")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1))
            .not_valid_after(now+timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("nas.example.test")]), critical=False)
            .sign(key, hashes.SHA256()))
    crt, private = path/(label+".pem"), path/(label+".key")
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, private)
    return ctx, crt, cert.fingerprint(hashes.SHA256()).hex()


def receive(stream, size):
    data = bytearray()
    while len(data) < size:
        part = stream.recv(size-len(data))
        if not part:
            raise OSError("early EOF")
        data.extend(part)
    return bytes(data)


class LocalServer(socketserver.ThreadingTCPServer):
    daemon_threads = True


def listen(stack, handler):
    server = stack.enter_context(LocalServer(("127.0.0.1", 0), handler))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stack.callback(server.shutdown)
    return server.server_address[1]


def run():
    with tempfile.TemporaryDirectory(prefix="drive-roaming-") as tmp, contextlib.ExitStack() as stack:
        base = Path(tmp)
        valid, cert, pin = certificate(base, "home")
        other, _, _ = certificate(base, "other")
        mode = ["home"]
        count = {"lan": 0, "vps": 0}
        class Identity(socketserver.BaseRequestHandler):
            def handle(self):
                if mode[0] == "away":
                    return
                with contextlib.suppress(OSError):
                    with (valid if mode[0] == "home" else other).wrap_socket(self.request, server_side=True):
                        pass
        def echo(route):
            class Echo(socketserver.BaseRequestHandler):
                def handle(self):
                    with contextlib.suppress(OSError):
                        with valid.wrap_socket(self.request, server_side=True) as stream:
                            stream.settimeout(6)
                            size = int.from_bytes(receive(stream, 4), "big")
                            if size > 1048576:
                                return
                            value = receive(stream, size)
                            count[route] += 1
                            stream.sendall(hashlib.sha256(value).digest() + value)
            return Echo
        verify_port = listen(stack, Identity)
        lan_port = listen(stack, echo("lan"))
        bridge = listen(stack, echo("vps"))
        host_path = base/"host"
        host_key = ed25519.Ed25519PrivateKey.generate()
        host_path.write_bytes(host_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()))
        host = paramiko.Ed25519Key.from_private_key_file(str(host_path))
        allowed = []
        class Auth(paramiko.ServerInterface):
            def check_auth_publickey(self, username, key):
                return paramiko.AUTH_SUCCESSFUL if username == "dr-client" and key.get_base64() in allowed else paramiko.AUTH_FAILED
            def get_allowed_auths(self, username):
                return "publickey"
            def check_channel_direct_tcpip_request(self, channel_id, origin, dest):
                return paramiko.OPEN_SUCCEEDED if dest == ("127.0.0.1", bridge) else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        class SSH(socketserver.BaseRequestHandler):
            @staticmethod
            def forward(ch):
                with contextlib.suppress(OSError, EOFError), ch, socket.create_connection(("127.0.0.1", bridge)) as dest:
                    def pump(src, dst):
                        with contextlib.suppress(OSError, EOFError):
                            while True:
                                chunk = src.recv(65536)
                                if not chunk:
                                    break
                                dst.sendall(chunk)
                            dst.shutdown(socket.SHUT_WR)
                    thread = threading.Thread(target=pump, args=(ch, dest), daemon=True)
                    thread.start()
                    pump(dest, ch)
                    thread.join(timeout=2)
            def handle(self):
                with contextlib.suppress(OSError, EOFError, paramiko.SSHException):
                    with paramiko.Transport(self.request) as tr:
                        tr.add_server_key(host)
                        tr.start_server(server=Auth())
                        while tr.is_active():
                            ch = tr.accept(.5)
                            if ch:
                                threading.Thread(target=self.forward,args=(ch,),daemon=True).start()
        ssh = listen(stack, SSH)
        with socket.socket() as spare:
            spare.bind(("127.0.0.1", 0))
            fixed = spare.getsockname()[1]
        state = base/"endpoint"
        routes = {"mode":"auto", "lan_ip":"127.0.0.1", "nas_name":"nas.example.test",
                  "drive_port":lan_port, "verify_port":verify_port, "cert_sha256":pin}
        invite = {"schema":"drive-ssh-relay/v2", "host":"127.0.0.1", "host_key":"ssh-ed25519 "+host.get_base64(),
                  "ssh_port":ssh,"bridge_port":bridge,"routing":routes}
        invitation = base/"invitation.json"
        invitation.write_text(json.dumps(invite))
        cli = [sys.executable, "-B", str(ROOT/"relay.py"), "--state", str(state)]
        subprocess.run([*cli, "init", "client", "--invite", str(invitation), "--local-port", str(fixed)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **config.quiet_kwargs())
        allowed.append((state/"identity.pub").read_text().split()[1])
        proc = subprocess.Popen([*cli, "run"], **config.quiet_kwargs())
        def stop():
            if proc.poll() is None:
                (state/"stop.request").touch()
                try:
                    proc.wait(8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        stack.callback(stop)
        client_tls = ssl.create_default_context(cafile=str(cert))
        def wait_for(route, reason=None):
            deadline = time.monotonic()+24
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError("supervisor exited " + str(proc.returncode))
                try:
                    snapshot = json.loads((state/"status.json").read_text())
                    if snapshot.get("route")==route and (not reason or snapshot.get("route_reason")==reason):
                        return
                except (OSError, ValueError):
                    pass
                time.sleep(.2)
            raise AssertionError((route, reason, onboard.describe(state)))
        def exchange(route):
            before = count.copy()
            payload = os.urandom(262144)
            with socket.create_connection(("127.0.0.1", fixed), timeout=10) as conn:
                with client_tls.wrap_socket(conn, server_hostname="nas.example.test") as tls:
                    tls.sendall(len(payload).to_bytes(4,"big")+payload)
                    assert receive(tls, len(payload)+32) == hashlib.sha256(payload).digest()+payload
            assert count[route] == before[route]+1 and sum(count.values()) == sum(before.values())+1
        wait_for("lan")
        exchange("lan")
        # An old live socket must be closed so Drive reconnects through the new path.
        with socket.create_connection(("127.0.0.1", fixed), timeout=10) as alive:
            with client_tls.wrap_socket(alive, server_hostname="nas.example.test") as session:
                mode[0] = "away"
                wait_for("vps")
                try:
                    assert session.recv(1) == b""
                except (ConnectionError, ssl.SSLError):
                    pass
        exchange("vps")
        mode[0] = "wrong"
        wait_for("vps", "nas_identity_mismatch")
        exchange("vps")
        mode[0] = "home"
        wait_for("lan")
        exchange("lan")
        subprocess.run([*cli,"config","set","--mode","relay"], check=True, stdout=subprocess.DEVNULL, **config.quiet_kwargs())
        wait_for("vps", "relay_selected")
        exchange("vps")
        subprocess.run([*cli,"config","set","--mode","lan"], check=True, stdout=subprocess.DEVNULL, **config.quiet_kwargs())
        wait_for("lan", "home_verified")
        exchange("lan")
        mode[0] = "wrong"
        wait_for("unavailable", "nas_identity_mismatch")
        before = count.copy()
        with socket.create_connection(("127.0.0.1", fixed), timeout=4) as conn:
            try:
                assert conn.recv(1) == b""
            except ConnectionResetError:
                pass
        assert count == before
        mode[0] = "home"
        wait_for("lan", "home_verified")
        exchange("lan")
        print("passed: automatic roaming, manual VPS and manual LAN; wrong NAS blocked in manual LAN, recovery, fixed endpoint, TLS payloads and reconnect", flush=True)


if __name__ == "__main__":
    run()
