"""Install the management services on a Debian/Ubuntu system from a root-owned release."""

import argparse
import os
from pathlib import Path
import pwd
import re
import socket
import subprocess
import sys
import tempfile

CONFIG = Path("/etc/drive-relay-fleet")
DATA = Path("/var/lib/drive-relay-fleet")
APP = Path("/opt/drive-relay-fleet")
SYSTEM = Path("/etc/systemd/system")
HERE = Path(__file__).resolve().parent


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def put(path, content, mode=0o644, user=None):
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        os.chmod(name, mode)
        if user:
            os.chown(name, user.pw_uid, user.pw_gid)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def account(label, comment, allow_standard=False):
    try:
        info = pwd.getpwnam(label)
    except KeyError:
        run(
            "useradd",
            "--system",
            "--no-create-home",
            "--home-dir",
            "/nonexistent",
            "--shell",
            "/usr/sbin/nologin",
            "--comment",
            comment,
            label,
        )
        if label == "dr-client":
            run("usermod", "--password", "*", label)
        return pwd.getpwnam(label)
    if info.pw_shell != "/usr/sbin/nologin" or info.pw_gecos not in (
        {comment, "Drive SSH Relay"} if allow_standard else {comment}
    ):
        raise ValueError("existing system user has another owner: " + label)
    return info


def main():
    arg = argparse.ArgumentParser()
    arg.add_argument("--domain", required=True)
    arg.add_argument("--invite", type=Path, required=True)
    arg.add_argument("--host-key", type=Path, required=True)
    arg.add_argument("--https-port", type=int, default=8443)
    args = arg.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("run as root")
    release = HERE.parents[1]
    if release.parent != APP / "releases" or release.stat().st_uid != 0:
        raise ValueError("install from a root-owned release under /opt/drive-relay-fleet/releases")
    sys.path.insert(0, str(release))
    from drive_relay import onboard
    from drive_relay.config import hostname, port, public_key

    domain = hostname(args.domain)
    https = port(args.https_port)
    invitation = onboard.read_invite(args.invite)
    profile, routes = onboard.parse_invite(invitation)
    key = args.host_key.resolve(strict=True)
    if not re.fullmatch(r"/[A-Za-z0-9/_.-]+", str(key)) or key.stat().st_uid != 0:
        raise ValueError("invalid SSH service host-key path")
    if (
        public_key(run("ssh-keygen", "-y", "-f", str(key), capture_output=True, text=True).stdout)
        != profile.host_key
    ):
        raise ValueError("invited VPS key differs from the serving key")
    if len({https, profile.ssh_port, 18441, profile.bridge_port}) != 4:
        raise ValueError("port conflict")
    cert = Path("/etc/letsencrypt/live") / domain / "fullchain.pem"
    if not cert.is_file():
        raise ValueError("provision the HTTPS certificate first")
    new = not (CONFIG / "settings").exists()
    if new:
        for p in (https, profile.ssh_port, 18441):
            with socket.socket() as probe:
                probe.bind(("0.0.0.0", p))
    usr = account("drive-fleet", "Drive Relay device management")
    account("dr-client", "Drive Relay restricted client", True)
    CONFIG.mkdir(exist_ok=True)
    DATA.mkdir(exist_ok=True, mode=0o700)
    os.chown(DATA, usr.pw_uid, usr.pw_gid)
    download = DATA / "downloads"
    download.mkdir(exist_ok=True)
    os.chown(download, usr.pw_uid, usr.pw_gid)
    put(download / "drive-ssh-relay.zip", (release / "client.zip").read_bytes(), 0o600, usr)
    put(DATA / "invite.json", args.invite.read_bytes(), 0o600, usr)
    authority = domain if https == 443 else domain + ":" + str(https)
    put(CONFIG / "settings", ("ORIGIN=https://" + authority + "\n").encode())
    web = (
        (HERE / "web.conf.in")
        .read_text()
        .replace("@DOMAIN@", domain)
        .replace("@AUTHORITY@", authority)
        .replace("@HTTPS_PORT@", str(https))
    )
    ssh = (
        (HERE / "sshd_config.in")
        .read_text()
        .replace("@CLIENT_PORT@", str(profile.ssh_port))
        .replace("@HOST_KEY@", str(key))
        .replace("@BRIDGE_PORT@", str(profile.bridge_port))
    )
    put(CONFIG / "web.conf", web.encode())
    put(CONFIG / "sshd_config", ssh.encode())
    path = APP / "current"
    temp = APP / ".next"
    if path.exists() and not path.is_symlink():
        raise ValueError("unexpected application directory")
    temp.unlink(missing_ok=True)
    temp.symlink_to(release)
    os.replace(temp, path)
    (HERE / "authorized-keys").chmod(0o755)
    run("/usr/sbin/nginx", "-t", "-c", str(CONFIG / "web.conf"))
    run("/usr/sbin/sshd", "-t", "-f", str(CONFIG / "sshd_config"))
    for unit in HERE.glob("*.service"):
        put(SYSTEM / unit.name, unit.read_bytes())
    hooks = Path("/etc/letsencrypt/renewal-hooks/deploy")
    hooks.mkdir(parents=True, exist_ok=True)
    put(
        hooks / "drive-relay-fleet",
        (
            '#!/bin/sh\nif [ "$RENEWED_LINEAGE" = "/etc/letsencrypt/live/'
            + domain
            + '" ]; then systemctl reload drive-relay-fleet-web.service; fi\n'
        ).encode(),
        0o755,
    )
    run("systemctl", "daemon-reload")
    for u in ("drive-relay-fleet", "drive-relay-client-sshd", "drive-relay-fleet-web"):
        run("systemctl", "enable", u, stdout=subprocess.DEVNULL)
        if (
            not new
            and u in ("drive-relay-client-sshd", "drive-relay-fleet-web")
            and subprocess.run(["systemctl", "is-active", "--quiet", u]).returncode == 0
        ):
            if u.endswith("web"):
                run("systemctl", "reload", u)
            else:
                run("systemctl", "kill", "--kill-who=main", "--signal=HUP", u)
        else:
            run("systemctl", "restart", u)
    print("Management URL https://" + authority + "/")


if __name__ == "__main__":
    main()
