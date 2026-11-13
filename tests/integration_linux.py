"""Run only on a disposable Ubuntu runner with systemd and openssh-server."""
import contextlib
import hashlib
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
        def exchange():
            with socket.create_connection(("127.0.0.1",local),timeout=3) as stream:
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
        host = Path("/etc/drive-ssh-relay/host_ed25519.pub")
        profiles = {}
        for role in ("nas","client"):
            state = base/(role+" state")
            profiles[role]=state
            v = [sys.executable,str(CLI),"--state",str(state)]
            command(*v,"init",role,"--host","127.0.0.1","--host-key-file",str(host),
                    "--ssh-port",str(ssh),"--bridge-port",str(bridge),"--drive-port",str(drive),"--local-port",str(local))
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
        def pid(role):
            return json.loads((profiles[role]/"status.json").read_text())["ssh_pid"]
        original = {role:eventually(lambda r=role:pid(r)) for role in profiles}
        command("systemctl","restart","drive-ssh-relay.service")
        eventually(lambda: all(pid(r) and pid(r)!=old for r,old in original.items()))
        eventually(exchange)
        print("passed: both endpoints reconnect after VPS restart",flush=True)
        # Drop one relay leg and require the other one to survive and recover.
        bridge_pid=pid("nas")
        os.kill(bridge_pid,signal.SIGTERM)
        eventually(lambda:pid("nas") and pid("nas")!=bridge_pid)
        eventually(exchange)
        vps("revoke","client","client-one")
        os.kill(pid("client"),signal.SIGTERM)
        def rejected():
            return json.loads((profiles["client"]/"status.json").read_text())["detail"]=="key_rejected"
        eventually(rejected)
        print("passed: NAS reconnect, key revocation and explicit diagnostic",flush=True)
    assert not Path("/etc/drive-ssh-relay").exists()
    print("passed: uninstall and cleanup",flush=True)


if __name__ == "__main__":
    run()
