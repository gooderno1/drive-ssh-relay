import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from . import __version__, onboard, panel, probe, runtime, services
from .config import Profile, fingerprint, initialize, load
from .routing import Routes, save, settings
from dataclasses import asdict


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
    source=init.add_mutually_exclusive_group(required=True)
    source.add_argument("--host")
    source.add_argument("--invite", type=Path, help="public JSON enrollment file from a trusted VPS administrator")
    init.add_argument("--host-key-file", type=Path, help="public host key when configuring --host manually")
    for arg, default in (("ssh-port",None),("bridge-port",None),("local-port",16690),("drive-port",6690)):
        init.add_argument("--" + arg, type=int, default=default)
    sub.add_parser("run", help="run this endpoint supervisor in the foreground")
    sub.add_parser("doctor", help="inspect network layers, listener and fresh supervisor state")
    sub.add_parser("status", help="JSON status of the current profile, including selected route")
    sub.add_parser("capabilities", help="machine-readable interface and available actions")
    fleet=sub.add_parser("fleet",help="pair a desktop with a management server").add_subparsers(dest="action",required=True)
    pair=fleet.add_parser("join");pair.add_argument("--url",required=True);pair.add_argument("--code-stdin",action="store_true",required=True);pair.add_argument("--local-port",type=int,default=16690)
    for call in ("sync","watch","install","uninstall"):fleet.add_parser(call)
    for name in ("preflight", "setup"):
        cmd = sub.add_parser(name, help="check desktop prerequisites" if name == "preflight" else "check and enroll a new desktop from an invitation")
        cmd.add_argument("--local-port", type=int, default=16690)
        cmd.add_argument("--drive-path", help="installed Drive executable or macOS application at a custom location")
        if name == "setup":
            cmd.add_argument("--invite", required=True, type=Path)
    route = sub.add_parser("config", help="show or update desktop routing without changing the fixed Drive endpoint")
    action = route.add_subparsers(dest="action", required=True)
    action.add_parser("show")
    update = action.add_parser("set")
    update.add_argument("--mode", choices=["auto", "relay", "lan"])
    for param in ("lan-ip", "nas-name", "cert-sha256"):
        update.add_argument("--" + param)
    for param in ("drive-port", "verify-port"):
        update.add_argument("--" + param, type=int)
    ui = sub.add_parser("panel", help="serve the private local configuration page until stopped")
    ui.add_argument("--port", type=int, default=0)
    ui.add_argument("--open", action="store_true", help="open the private URL in your browser")
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


def profile_from_args(args):
    if args.invite:
        if args.host_key_file or args.ssh_port is not None or args.bridge_port is not None:
            raise ValueError("the invitation already supplies the host key and VPS ports")
        return onboard.parse_invite(onboard.read_invite(args.invite), args.role, args.local_port, args.drive_port)[0]
    if not args.host_key_file:
        raise ValueError("manual configuration needs --host-key-file")
    return Profile(args.role,args.host,args.host_key_file.read_text(encoding="utf-8-sig"),
                   443 if args.ssh_port is None else args.ssh_port,
                   16690 if args.bridge_port is None else args.bridge_port,args.local_port,args.drive_port)


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    state = args.state.expanduser().resolve()
    try:
        if args.cmd == "init":
            cfg = profile_from_args(args)
            key = initialize(state, cfg)
            if args.invite and args.role == "client" and not (state/"routing.json").exists():
                save(state, cfg, onboard.parse_invite(onboard.read_invite(args.invite))[1])
            print("Pinned VPS host key " + fingerprint(cfg.host_key), file=sys.stderr)
            print(key)
        elif args.cmd == "run":
            return runtime.run(state)
        elif args.cmd == "doctor":
            return runtime.doctor(state)
        elif args.cmd == "status":
            print(json.dumps(onboard.describe(state), indent=2))
        elif args.cmd == "capabilities":
            print(json.dumps(dict(schema=onboard.SCHEMA, version=__version__,
                  commands=["preflight", "setup", "status", "doctor", "config show", "config set", "service", "panel", "probe", "fleet"],
                  modes=["auto", "relay", "lan"], exit_codes={"0":"success", "1":"invalid input or runtime error",
                  "2":"prerequisite, health or verification incomplete"}), indent=2))
        elif args.cmd == "preflight":
            result = onboard.preflight(state, args.local_port, args.drive_path)
            print(json.dumps(result, indent=2))
            return 0 if result["ready"] else 2
        elif args.cmd == "setup":
            result = onboard.setup(state, onboard.read_invite(args.invite), args.local_port, args.drive_path)
            print(json.dumps(result, indent=2))
            return 2 if result["status"] == "prerequisites" else 0
        elif args.cmd == "config":
            profile = load(state)
            current = asdict(settings(state))
            if args.action == "set":
                current.update({key: getattr(args, key) for key in current if getattr(args, key, None) is not None})
                save(state, profile, Routes(**current))
            print(json.dumps(asdict(settings(state)), indent=2))
        elif args.cmd == "fleet":
            from . import managed
            if args.action=="join":result=managed.join(state,args.url,sys.stdin.readline().strip(),args.local_port)
            elif args.action=="watch":return managed.watch(state)
            elif args.action=="sync":result=managed.sync(state)
            else:result=managed.service(state,args.action)
            print(json.dumps(result))
        elif args.cmd == "panel":
            panel.run(state, args.port, args.open)
        elif args.cmd == "service":
            services.service(args.action, state)
        elif args.action == "send":
            return probe.send(args.folder, args.mib)
        else:
            return probe.verify(args.folder, args.name, getattr(args,"timeout",0), args.action=="clean")
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as e:
        if args.cmd in ("preflight", "setup", "config", "status", "capabilities", "doctor"):
            print(json.dumps({"schema": onboard.SCHEMA, "error": str(e)}))
            return 1
        if isinstance(e, subprocess.CalledProcessError) and e.stderr:
            detail = e.stderr.decode(errors="replace").strip() if isinstance(e.stderr, bytes) else e.stderr.strip()
            p.exit(1, f"drive-ssh-relay: {detail}\n")
        p.exit(1, f"drive-ssh-relay: {e}\n")
    return 0
