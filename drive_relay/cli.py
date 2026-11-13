import argparse
import os
from pathlib import Path
import subprocess
import sys

from . import __version__, probe, runtime, services
from .config import Profile, fingerprint, initialize


def default_state():
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "DriveSSHRelay"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "DriveSSHRelay"
    return Path.home() / ".local" / "share" / "drive-ssh-relay"


def parser():
    p = argparse.ArgumentParser(description="Self-hosted SSH transport for Synology Drive")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--state", type=Path, default=default_state(), help="private endpoint state (outside sync folders)")
    sub = p.add_subparsers(dest="cmd", required=True)
    init = sub.add_parser("init", help="enroll an endpoint and print its public device key")
    init.add_argument("role", choices=["nas", "client"])
    init.add_argument("--host", required=True)
    init.add_argument("--host-key-file", required=True, type=Path, help="public host key from the VPS administrator")
    for arg, default in (("ssh-port",443),("bridge-port",16690),("local-port",16690),("drive-port",6690)):
        init.add_argument("--" + arg, type=int, default=default)
    sub.add_parser("run", help="run this endpoint supervisor in the foreground")
    sub.add_parser("doctor", help="inspect network layers, listener and fresh supervisor state")
    svc = sub.add_parser("service", help="manage the endpoint background service")
    svc.add_argument("action", choices=["install","start","stop","uninstall"])
    pr = sub.add_parser("probe", help="verify an actual file through two synchronized folders")
    cmds = pr.add_subparsers(dest="action", required=True)
    for key in ("send","ack","clean"):
        cmd = cmds.add_parser(key)
        cmd.add_argument("--folder", required=True, type=Path)
        if key == "send":
            cmd.add_argument("--mib", type=int, default=1)
        else:
            cmd.add_argument("--name", required=True)
            if key == "ack":
                cmd.add_argument("--timeout", type=int, default=120)
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    state = args.state.expanduser().resolve()
    try:
        if args.cmd == "init":
            cfg = Profile(args.role, args.host, args.host_key_file.read_text(), args.ssh_port,
                          args.bridge_port, args.local_port, args.drive_port)
            key = initialize(state, cfg)
            print("Pinned VPS host key " + fingerprint(cfg.host_key), file=sys.stderr)
            print(key)
        elif args.cmd == "run":
            return runtime.run(state)
        elif args.cmd == "doctor":
            return runtime.doctor(state)
        elif args.cmd == "service":
            services.service(args.action, state)
        elif args.action == "send":
            return probe.send(args.folder, args.mib)
        else:
            return probe.verify(args.folder, args.name, getattr(args,"timeout",0), args.action=="clean")
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as e:
        if isinstance(e, subprocess.CalledProcessError) and e.stderr:
            detail = e.stderr.decode(errors="replace").strip() if isinstance(e.stderr, bytes) else e.stderr.strip()
            p.exit(1, f"drive-ssh-relay: {detail}\n")
        p.exit(1, f"drive-ssh-relay: {e}\n")
    return 0
