#!/usr/bin/env python3
"""Operate only the dedicated relay on a Debian/Ubuntu systemd VPS."""
import argparse
import base64
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

BASE = Path("/etc/drive-ssh-relay")
UNIT = Path("/etc/systemd/system/drive-ssh-relay.service")
HERE = Path(__file__).resolve().parent
USERS = {"nas": "dr-nas", "client": "dr-client"}
STAMP = "Drive SSH Relay v1\n"


def call(*args, capture=False):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE if capture else None)


def write(path, data, mode):
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def owned():
    marker = BASE / ".managed"
    return marker.is_file() and marker.read_text() == STAMP


def lookup(user):
    try:
        return pwd.getpwnam(user)
    except KeyError:
        return None


def reserved_account(user):
    row = lookup(user)
    return row and row.pw_dir == "/nonexistent" and row.pw_gecos == "Drive SSH Relay" and row.pw_shell.endswith("/nologin")


def number(n):
    n = int(n)
    if not 1 <= n <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return n


def ports():
    return tuple(map(int, (BASE / "ports").read_text().split()))


def ed25519(data):
    fields = data.strip().split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519" or "\n" in data.strip():
        raise ValueError("expected one OpenSSH Ed25519 public key")
    raw = base64.b64decode(fields[1], validate=True)
    if len(raw) != 51 or raw[:19] != b"\0\0\0\x0bssh-ed25519\0\0\0\x20":
        raise ValueError("invalid Ed25519 public key")
    return "ssh-ed25519 " + fields[1]


def install(args):
    ssh_port, bridge = number(args.ssh_port), number(args.bridge_port)
    if ssh_port == bridge:
        raise ValueError("public SSH and loopback bridge ports must differ")
    first = not BASE.exists()
    if not first and not owned():
        raise ValueError("existing configuration directory belongs to another installation")
    if UNIT.exists() and (first or UNIT.read_bytes() != (HERE / UNIT.name).read_bytes()):
        raise ValueError("existing service unit differs, inspect it before installation")
    if not first and (BASE / "ports").exists() and ports() != (ssh_port, bridge):
        raise ValueError("port change requires an explicit migration; see docs/operations.md")
    for user in USERS.values():
        if lookup(user) and (first or not reserved_account(user)):
            raise ValueError("existing account collision: " + user)
    for exe in ("ssh-keygen", "ss", "systemctl", "useradd"):
        if not shutil.which(exe):
            raise ValueError("missing prerequisite: " + exe)
    if not Path("/usr/sbin/sshd").is_file() or not Path("/run/systemd/system").is_dir():
        raise ValueError("requires openssh-server and running systemd")
    running = (not first and subprocess.run(["systemctl","is-active","--quiet",UNIT.name]).returncode == 0)
    if not running:
        for host, port in (("0.0.0.0", ssh_port), ("127.0.0.1", bridge)):
            with socket.socket() as s:
                s.bind((host, port))
    if first:
        BASE.mkdir(mode=0o755)
        write(BASE / ".managed", STAMP.encode(), 0o644)
    # Safe to resume after interruption. Retain enrolled keys and the host identity.
    BASE.chmod(0o755)
    write(BASE / "ports", f"{ssh_port} {bridge}\n".encode(), 0o644)
    keys = BASE / "keys"
    keys.mkdir(exist_ok=True)
    keys.chmod(0o755)
    for user in USERS.values():
        if not lookup(user):
            call("useradd", "--system", "--no-create-home", "--home-dir", "/nonexistent",
                 "--shell", "/usr/sbin/nologin", "--comment", "Drive SSH Relay", user)
        # Portable sshd without PAM refuses locked shadow accounts, even for keys.
        # "*" cannot authenticate any password. "!" marks a locked account on Linux.
        # AuthenticationMethods publickey and nologin also apply to both roles.
        call("usermod", "--password", "*", user)
        if not (keys / user).exists():
            write(keys / user, b"", 0o644)
    key = BASE / "host_ed25519"
    if not key.exists():
        call("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "drive-relay", "-f", str(key))
    pub = call("ssh-keygen", "-y", "-f", str(key), capture=True).stdout
    write(key.with_suffix(".pub"), (ed25519(pub) + "\n").encode(), 0o644)
    text = (HERE / "sshd_config.in").read_text().replace("@SSH_PORT@", str(ssh_port)).replace("@BRIDGE_PORT@", str(bridge))
    # Distribution sshd uses this directory during privilege separation.
    Path("/run/sshd").mkdir(mode=0o755, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=BASE, mode="w", encoding="ascii") as testconf:
        testconf.write(text)
        testconf.flush()
        call("/usr/sbin/sshd", "-t", "-f", testconf.name)
    cfg = BASE / "sshd_config"
    changed = not cfg.exists() or cfg.read_bytes() != text.encode()
    write(cfg, text.encode(), 0o644)
    write(UNIT, (HERE / UNIT.name).read_bytes(), 0o644)
    call("systemctl", "daemon-reload")
    call("systemctl", "enable", "--now", UNIT.name)
    if running and changed:
        call("systemctl","reload",UNIT.name)
    call("systemctl", "is-active", "--quiet", UNIT.name)
    print("Dedicated service started. Open the selected TCP port in the VPS and provider firewall.")
    print(pub.strip().split()[0] + " " + pub.strip().split()[1])
    call("ssh-keygen", "-lf", str(key.with_suffix(".pub")))


def enroll(args):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", args.name):
        raise ValueError("use a device label containing 1–40 lowercase letters, numbers or hyphens")
    path = BASE / "keys" / USERS[args.role]
    lines = path.read_text().splitlines()
    label = "device=" + args.name
    old = [line for line in lines if line.endswith(" " + label)]
    if args.command == "revoke":
        if not old:
            raise ValueError("unknown device label")
        lines = [line for line in lines if not line.endswith(" " + label)]
    else:
        key = ed25519(args.public_key.read_text(encoding="ascii"))
        option = "permitlisten" if args.role == "nas" else "permitopen"
        line = f'restrict,port-forwarding,{option}="127.0.0.1:{ports()[1]}" {key} {label}'
        if old == [line]:
            print("Device already enrolled.")
            return
        if old:
            raise ValueError("label already in use; enroll a new label, verify, then revoke the old one")
        for user in USERS.values():
            if any(key.split()[1] in row.split() for row in (BASE / "keys" / user).read_text().splitlines()):
                raise ValueError("public key is already enrolled; use a distinct device key")
        lines.append(line)
    write(path, ("\n".join(lines) + ("\n" if lines else "")).encode("ascii"), 0o644)
    print("Device key list updated. Revocation takes effect on new SSH sessions.")


def remove(args):
    # Fail closed on a replaced service or account. The host key is retained by default.
    if UNIT.exists() and UNIT.read_bytes() != (HERE / UNIT.name).read_bytes():
        raise ValueError("service file was changed; inspect it before removal")
    if any(lookup(u) and not reserved_account(u) for u in USERS.values()):
        raise ValueError("a role account was replaced; inspect it before removal")
    if UNIT.exists():
        call("systemctl", "disable", "--now", UNIT.name)
        UNIT.unlink()
        call("systemctl", "daemon-reload")
    if args.purge:
        for user in USERS.values():
            if lookup(user):
                for attempt in range(10):
                    result = subprocess.run(["userdel", user], capture_output=True, text=True)
                    if result.returncode == 0:
                        break
                    if result.returncode != 8 or attempt == 9:
                        raise ValueError(result.stderr.strip() or "userdel failed")
                    time.sleep(0.5)
        for item in (BASE/"keys"/"dr-nas", BASE/"keys"/"dr-client", BASE/"keys",
                     BASE/"sshd_config", BASE/"host_ed25519.pub", BASE/"host_ed25519",
                     BASE/"ports", BASE/".managed"):
            if item.is_dir():
                item.rmdir()
            else:
                item.unlink(missing_ok=True)
        BASE.rmdir()
    print("Service removed." + ("" if args.purge else " Enrollment and host key retained."))


def status():
    ssh_port, bridge = ports()
    running = subprocess.run(["systemctl","is-active","--quiet",UNIT.name]).returncode == 0
    def sockets(port):
        lines = call("ss","-H","-lnt","sport","=",":"+str(port),capture=True).stdout.splitlines()
        return [line.split()[3] for line in lines if len(line.split()) >= 4]
    public = sockets(ssh_port)
    bridge_addresses = sockets(bridge)
    private = bool(bridge_addresses) and all(s == f"127.0.0.1:{bridge}" for s in bridge_addresses)
    print(json.dumps({"service_active":running,"ssh_port_listening":bool(public),
                      "nas_loopback_bridge_ready":private,"bridge_addresses":bridge_addresses},indent=2))
    return 0 if running and public and private else 2


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("install")
    setup.add_argument("--ssh-port", type=number, default=443)
    setup.add_argument("--bridge-port", type=number, default=16690)
    for action in ("authorize", "revoke"):
        cmd = sub.add_parser(action)
        cmd.add_argument("role", choices=USERS)
        cmd.add_argument("name")
        if action == "authorize":
            cmd.add_argument("public_key", type=Path)
    sub.add_parser("status")
    sub.add_parser("host-key")
    remove_cmd = sub.add_parser("uninstall")
    remove_cmd.add_argument("--purge", action="store_true", help="also remove role accounts, public key lists and host key")
    args = p.parse_args()
    if os.geteuid() != 0:
        p.exit(1, "Use sudo on the VPS.\n")
    os.umask(0o077)
    try:
        with open("/run/drive-ssh-relay-admin.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if args.command == "install":
                install(args)
            elif not owned():
                raise ValueError("no managed installation")
            elif args.command in ("authorize", "revoke"):
                enroll(args)
            elif args.command == "host-key":
                print((BASE / "host_ed25519.pub").read_text().strip())
            elif args.command == "status":
                return status()
            else:
                remove(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        p.exit(1, f"drive-ssh-relay: {e}\n")


if __name__ == "__main__":
    sys.exit(main())
