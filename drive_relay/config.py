import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
from dataclasses import asdict, dataclass

LOOPBACK = "127.0.0.1"
USERS = {"nas": "dr-nas", "client": "dr-client"}


def port(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError("port must be an integer from 1 to 65535")
    return value


def hostname(text):
    if not isinstance(text, str) or len(text) > 253:
        raise ValueError("invalid VPS hostname or IPv4 address")
    try:
        ipaddress.IPv4Address(text)
        return text
    except ValueError:
        pass
    labels = text.split(".")
    if not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels):
        raise ValueError("use a DNS hostname or IPv4 address without a URL, username or port")
    return text.lower()


def public_key(text):
    fields = text.strip().split()
    if "\n" in text.strip() or len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise ValueError("expected one Ed25519 public key from the VPS console")
    raw = base64.b64decode(fields[1], validate=True)
    if len(raw) != 51 or raw[:19] != struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32):
        raise ValueError("invalid Ed25519 public key payload")
    return "ssh-ed25519 " + fields[1]


def fingerprint(key):
    digest = hashlib.sha256(base64.b64decode(public_key(key).split()[1])).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


@dataclass(frozen=True)
class Profile:
    role: str
    host: str
    host_key: str
    ssh_port: int = 443
    bridge_port: int = 16690
    local_port: int = 16690
    drive_port: int = 6690

    def __post_init__(self):
        if self.role not in USERS:
            raise ValueError("role must be nas or client")
        object.__setattr__(self, "host", hostname(self.host))
        object.__setattr__(self, "host_key", public_key(self.host_key))
        for n in (self.ssh_port, self.bridge_port, self.local_port, self.drive_port):
            port(n)
        if self.ssh_port == self.bridge_port:
            raise ValueError("public SSH and VPS bridge ports must differ")


def quiet_kwargs():
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": si}


def executable(name):
    if os.name == "nt" and name in ("ssh", "ssh-keygen"):
        path = Path(os.environ["WINDIR"]) / "System32" / "OpenSSH" / (name + ".exe")
        if path.is_file():
            return str(path)
    path = shutil.which(name)
    if not path:
        raise ValueError("missing executable: " + name)
    return path


def atomic(path, data, mode=0o600):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".relay-")
    try:
        with os.fdopen(fd, "wb") as f:
            if os.name != "nt":
                os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def load(state):
    entry = state / "profile.json"
    if not entry.is_file():
        raise ValueError("profile not enrolled; run init first")
    return Profile(**json.loads(entry.read_text(encoding="utf-8")))


def initialize(state, profile):
    """An interrupted enrollment can be retried with the same public parameters."""
    if not state.is_absolute():
        raise ValueError("use an absolute state directory")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt" and (state.stat().st_uid != os.geteuid() or state.stat().st_mode & 0o077):
        raise ValueError("state must be private and owned by this user (chmod 700)")
    if any(c in str(state) for c in '\r\n"%'):
        raise ValueError("unsupported character in state directory")
    existing = state / "profile.json"
    if not existing.exists() and any(f.name != ".enroll.lock" for f in state.iterdir()):
        raise ValueError("choose an empty, private state directory for a new profile")
    from .runtime import exclusive
    with exclusive(state / ".enroll.lock"):
        return enroll_files(state, profile)


def enroll_files(state, profile):
    existing = state / "profile.json"
    if existing.exists():
        if load(state) != profile:
            raise ValueError("this profile has different settings; use another state directory")
    else:
        if any(f.name != ".enroll.lock" for f in state.iterdir()):
            raise ValueError("choose an empty, private state directory for a new profile")
        atomic(existing, (json.dumps(asdict(profile), indent=2) + "\n").encode())
    key = state / "identity"
    pub = state / "identity.pub"
    if not key.exists():
        if pub.exists():
            raise ValueError("public key present but private key absent; restore the identity or enroll a new profile")
        subprocess.run([executable("ssh-keygen"), "-q", "-t", "ed25519", "-N", "",
                        "-C", "drive-relay-" + profile.role, "-f", str(key)],
                       check=True, stdin=subprocess.DEVNULL, **quiet_kwargs())
    derived = subprocess.check_output([executable("ssh-keygen"), "-y", "-f", str(key)],
                                      stdin=subprocess.DEVNULL, **quiet_kwargs()).decode()
    canonical = public_key(derived)
    if pub.exists() and public_key(pub.read_text()) != canonical:
        raise ValueError("public device key does not match the stored private key")
    atomic(pub, (canonical + "\n").encode(), 0o644)
    # HostKeyAlias is dedicated to this profile and is independent of SSH port formatting.
    atomic(state / "known_hosts", ("drive-relay-pin " + profile.host_key + "\n").encode())
    return canonical


def ssh_args(state, p):
    group = ["-F", "none", "-N", "-T", "-n", "-4", "-p", str(p.ssh_port), "-i", str(state/"identity")]
    if os.name == "nt":
        null_hosts = "NUL"
    else:
        null_hosts = "/dev/null"
    options = ["BatchMode=yes", "IdentitiesOnly=yes", "IdentityAgent=none",
               "StrictHostKeyChecking=yes", "HostKeyAlgorithms=ssh-ed25519", "UpdateHostKeys=no",
               "HostKeyAlias=drive-relay-pin", "GlobalKnownHostsFile=" + null_hosts,
               'UserKnownHostsFile="' + (state/"known_hosts").as_posix() + '"',
               "ConnectTimeout=8", "ConnectionAttempts=1", "ServerAliveInterval=20",
               "ServerAliveCountMax=3", "ExitOnForwardFailure=yes",
               "ForwardAgent=no", "ForwardX11=no", "ProxyCommand=none"]
    for opt in options:
        group.extend(["-o", opt])
    if p.role == "client":
        group.extend(["-L", f"{LOOPBACK}:{p.local_port}:{LOOPBACK}:{p.bridge_port}"])
    else:
        group.extend(["-R", f"{LOOPBACK}:{p.bridge_port}:{LOOPBACK}:{p.drive_port}"])
    return [executable("ssh"), *group, "-l", USERS[p.role], p.host]
