"""Run only on a disposable Ubuntu runner with systemd and openssh-server."""
import contextlib
import hashlib
import importlib.util
import os
from pathlib import Path
import pwd
import signal
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import json
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
VPS = ROOT/"deploy"/"vps"/"server.py"
CLI = ROOT/"relay.py"


def command(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def eventually(fn, seconds=70):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = fn()
            if result:
                return result
        except (OSError, ValueError, ssl.SSLError):
            pass
        time.sleep(.4)
    raise RuntimeError("timed out: " + fn.__name__)


def free_ports(count):
    with contextlib.ExitStack() as stack:
        ports = []
        for _ in range(count):
            s = stack.enter_context(socket.socket())
            s.bind(("127.0.0.1",0))
            ports.append(s.getsockname()[1])
        return ports


def exact(s, length):
    data = bytearray()
    while len(data) < length:
        chunk = s.recv(length-len(data))
        if not chunk:
            raise ConnectionError("incomplete message")
        data.extend(chunk)
    return bytes(data)


def stop(p):
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(8)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


def check_vps_replacement(base, profiles, processes, outer, drive, local, exchange):
    """Keep the original relay live while preparing a second SSH host on this runner."""
    ssh_b, bridge_b, preview_port = free_ports(3)
    spec = importlib.util.spec_from_file_location("candidate_vps", VPS)
    candidate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(candidate)

    def start_endpoint(state, cleanup):
        proc = subprocess.Popen([sys.executable, str(CLI), "--state", str(state), "run"])
        cleanup.callback(stop, proc)
        return proc

    with tempfile.TemporaryDirectory(dir=candidate.BASE, prefix="candidate-") as path, contextlib.ExitStack() as active:
        # The two sshd processes share Unix role accounts, but use separate
        # host keys, client authorizations, ports and NAS connections.
        root = Path(path)
        root.chmod(0o755)
        candidate.BASE = root
        (root/"ports").write_text(f"{ssh_b} {bridge_b}\n")
        auth = root/"keys"
        auth.mkdir()
        auth.chmod(0o755)
        for username in candidate.USERS.values():
            (auth/username).touch()
            (auth/username).chmod(0o644)
        command("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root/"host_ed25519"))
        config = (VPS.parent/"sshd_config.in").read_text()
        config = config.replace("/etc/drive-ssh-relay", str(root)).replace("/run/drive-ssh-relay/sshd.pid", str(root/"sshd.pid"))
        config = config.replace("@SSH_PORT@", str(ssh_b)).replace("@BRIDGE_PORT@", str(bridge_b))
        config = config.replace("ListenAddress 0.0.0.0", "ListenAddress 127.0.0.2")
        config_path = root/"sshd_config"
        config_path.write_text(config)
        command("/usr/sbin/sshd", "-t", "-f", str(config_path))
        daemon = subprocess.Popen(["/usr/sbin/sshd", "-D", "-f", str(config_path)])
        active.callback(stop, daemon)
        invite = base/"candidate-invite.json"
        invite.write_text(json.dumps(candidate.invitation("127.0.0.2")))
        assert json.loads(invite.read_text())["host_key"] != json.loads((base/"relay-invite.json").read_text())["host_key"]

        candidate_states = {}
        for label, role, port in (("nas-b", "nas", local), ("client-b", "client", local),
                                  ("preview-b", "client", preview_port)):
            state = base/label
            candidate_states[label] = state
            command(sys.executable, str(CLI), "--state", str(state), "init", role, "--invite", str(invite),
                    "--drive-port", str(drive), "--local-port", str(port))
            candidate.enroll(SimpleNamespace(command="authorize", role=role, name=label, public_key=state/"identity.pub"))

        old_profile = (profiles["client"]/"profile.json").read_bytes()
        new_nas = start_endpoint(candidate_states["nas-b"], active)
        preview = start_endpoint(candidate_states["preview-b"], active)
        eventually(lambda: exchange(preview_port))
        assert exchange(local)
        stop(preview)
        stop(processes["client"])
        new_client = start_endpoint(candidate_states["client-b"], active)
        eventually(lambda: exchange(local))
        # Prove that the unchanged Drive address now works without the old VPS.
        command("systemctl", "stop", "drive-ssh-relay.service")
        try:
            assert exchange(local)
        finally:
            command("systemctl", "start", "drive-ssh-relay.service")
        stop(new_client)
        stop(new_nas)
        stop(daemon)
        processes["client"] = start_endpoint(profiles["client"], outer)
        eventually(lambda: exchange(local))
        assert (profiles["client"]/"profile.json").read_bytes() == old_profile
        print("passed: independent VPS preflight, cutover with old VPS offline, rollback with new VPS offline, same Drive address and TLS identity", flush=True)


def run():
    if os.geteuid() != 0 or os.getenv("DRIVE_RELAY_DISPOSABLE") != "1":
        raise SystemExit("Use the dedicated disposable Linux CI runner.")
    if Path("/etc/drive-ssh-relay").exists() or Path("/etc/systemd/system/drive-ssh-relay.service").exists():
        raise SystemExit("refusing to touch an existing relay")
    for user in ("dr-client","dr-nas"):
        try:
            pwd.getpwnam(user)
        except KeyError:
            continue
        raise SystemExit("role account already exists")
    ssh, bridge, drive, local = free_ports(4)
    request, response = os.urandom(1024*1024), os.urandom(256*1024)
    with tempfile.TemporaryDirectory(prefix="drive-relay-it-") as tmp, contextlib.ExitStack() as stack:
        base = Path(tmp)
        cert, key = base/"cert", base/"server.key"
        command("openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1","-subj","/CN=drive.example.test",
                "-addext","subjectAltName=DNS:drive.example.test","-keyout",str(key),"-out",str(cert),stderr=subprocess.DEVNULL)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert,key)
        client_tls = ssl.create_default_context(cafile=str(cert))
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    with tls.wrap_socket(self.request, server_side=True) as s:
                        s.settimeout(6)
                        digest = hashlib.sha256(exact(s,len(request))).digest()
                        s.sendall(digest + response)
                except (OSError,ssl.SSLError):
                    pass
        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
        responder = stack.enter_context(Server(("127.0.0.1",drive),Handler))
        threading.Thread(target=responder.serve_forever,daemon=True).start()
        stack.callback(responder.shutdown)
        def exchange(port=local):
            with socket.create_connection(("127.0.0.1",port),timeout=3) as stream:
                with client_tls.wrap_socket(stream,server_hostname="drive.example.test") as conn:
                    conn.settimeout(6)
                    conn.sendall(request)
                    return exact(conn,32+len(response)) == hashlib.sha256(request).digest()+response
        def vps(*args):
            return command(sys.executable,str(VPS),*args)
        def cleanup():
            if Path("/etc/drive-ssh-relay/.managed").is_file():
                vps("uninstall","--purge")
        stack.callback(cleanup)
        vps("install","--ssh-port",str(ssh),"--bridge-port",str(bridge))
        vps("install","--ssh-port",str(ssh),"--bridge-port",str(bridge))
        invite = base/"relay-invite.json"
        with invite.open("w") as out:
            command(sys.executable,str(VPS),"invite","--host","127.0.0.1",stdout=out)
        profiles = {}
        for role in ("nas","client"):
            state = base/(role+" state")
            profiles[role]=state
            v = [sys.executable,str(CLI),"--state",str(state)]
            command(*v,"init",role,"--invite",str(invite),"--drive-port",str(drive),"--local-port",str(local))
            vps("authorize",role,role+"-one",str(state/"identity.pub"))
            vps("authorize",role,role+"-one",str(state/"identity.pub"))
        # Check the effective deployed policy, including the literal loopback port.
        for user, direction, scope in (("dr-nas","remote","permitlisten"),("dr-client","local","permitopen")):
            matches=command("/usr/sbin/sshd","-T","-f","/etc/drive-ssh-relay/sshd_config","-C",
                f"user={user},host=localhost,addr=127.0.0.1",capture_output=True).stdout
            assert f"allowtcpforwarding {direction}\n" in matches
            assert f"{scope} 127.0.0.1:{bridge}\n" in matches and "maxsessions 0\n" in matches
        processes = {}
        for role,state in profiles.items():
            proc = subprocess.Popen([sys.executable,str(CLI),"--state",str(state),"run"])
            processes[role] = proc
            stack.callback(stop,proc)
        eventually(exchange)
        assert not any(p.poll() is not None for p in processes.values())
        listeners=command("ss","-lntH","sport","=",":"+str(bridge),capture_output=True).stdout.splitlines()
        assert listeners and all(line.split()[3] == f"127.0.0.1:{bridge}" for line in listeners)
        print("passed: TLS hostname and data integrity in both directions",flush=True)
        check_vps_replacement(base, profiles, processes, stack, drive, local, exchange)
        # A syntactically valid but incorrect trusted key must fail before any forwarding.
        wrong = base/"wrong-host"
        spare = free_ports(1)[0]
        command(sys.executable,str(CLI),"--state",str(wrong),"init","client","--host","127.0.0.1",
                "--host-key-file",str(profiles["nas"]/"identity.pub"),"--ssh-port",str(ssh),
                "--bridge-port",str(bridge),"--local-port",str(spare))
        wrong_pin = (wrong/"known_hosts").read_bytes()
        reject = subprocess.Popen([sys.executable,str(CLI),"--state",str(wrong),"run"])
        stack.callback(stop,reject)
        def denied(state, port, detail):
            try:
                if exchange(port):
                    raise AssertionError("unauthorized stream passed TLS exchange")
            except (OSError, ssl.SSLError):
                pass
            time.sleep(.2)
            return json.loads((state/"status.json").read_text())["detail"] == detail
        def fail_closed():
            return denied(wrong, spare, "host_key_mismatch")
        eventually(fail_closed)
        assert (wrong/"known_hosts").read_bytes()==wrong_pin
        stop(reject)
        print("passed: wrong host key fails closed without replacing the pin",flush=True)
        def pid(role):
            return json.loads((profiles[role]/"status.json").read_text())["ssh_pid"]
        original = eventually(lambda: pid("nas"))
        command("systemctl","restart","drive-ssh-relay.service")
        def new_sessions():
            return pid("nas") and pid("nas") != original
        try:
            eventually(new_sessions)
        except RuntimeError:
            for role,state in profiles.items():
                print(role, "previous NAS pid", original, (state/"status.json").read_text(), flush=True)
                print((state/"events.log").read_text()[-1400:],flush=True)
            raise
        eventually(exchange)
        print("passed: both endpoints reconnect after VPS restart",flush=True)
        # Drop one relay leg and require the other one to survive and recover.
        bridge_pid=pid("nas")
        os.kill(bridge_pid,signal.SIGTERM)
        eventually(lambda:pid("nas") and pid("nas")!=bridge_pid)
        eventually(exchange)
        vps("revoke","client","client-one")
        os.kill(eventually(lambda: pid("client")), signal.SIGTERM)
        def rejected():
            return denied(profiles["client"], local, "key_rejected")
        eventually(rejected)
        print("passed: NAS reconnect, key revocation and explicit diagnostic",flush=True)
    assert not Path("/etc/drive-ssh-relay").exists()
    print("passed: uninstall and cleanup",flush=True)


if __name__ == "__main__":
    run()
