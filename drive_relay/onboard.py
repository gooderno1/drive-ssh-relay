"""Inventory and enrollment shared by the control page and noninteractive CLI."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import sys

from . import __version__, runtime
from .config import Profile, executable, fingerprint, initialize, load, port
from .routing import Routes, save, settings
from .observed import transport

SCHEMA = "drive-ssh-relay/desktop-v1"
DOWNLOAD = "https://www.synology.com/support/download"


def drive_client(override=None):
    roots = []
    if sys.platform == "win32":
        for variable in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(variable):
                roots.append(Path(os.environ[variable]) / "SynologyDrive" / "SynologyDrive.app" / "bin")
                roots.append(Path(os.environ[variable]) / "Synology" / "SynologyDrive" / "bin")
        names = ("cloud-drive-daemon.exe", "cloud-drive-ui.exe", "cloud-drive-connect.exe")
    elif sys.platform == "darwin":
        roots = [Path("/Applications"), Path.home() / "Applications"]
        names = ("Synology Drive Client.app", "Synology Drive.app")
    else:
        roots = [Path("/usr/bin"), Path("/opt/Synology/SynologyDrive/bin")]
        names = ("synology-drive", "cloud-drive-ui", "cloud-drive-daemon")
    candidates = [Path(override).expanduser()] if override else []
    candidates.extend(root / name for root in roots for name in names)
    for path in candidates:
        if path.name.lower() not in [n.lower() for n in names]:
            continue
        if path.is_file() or (path.suffix == ".app" and (path / "Contents/Info.plist").is_file()):
            return str(path)
    return None


def preflight(state, local_port, drive_path=None):
    port(local_port)
    checks = []
    def add(name, ok, action):
        checks.append(dict(id=name, ok=bool(ok), action="" if ok else action))
    add("python", sys.version_info >= (3, 9), "Install Python 3.9 or later from python.org.")
    for name in ("ssh", "ssh-keygen"):
        try:
            found = executable(name)
        except ValueError:
            found = None
        add(name, found, "Install the OpenSSH Client for this operating system.")
    if sys.platform == "win32":
        add("pythonw", Path(sys.executable).with_name("pythonw.exe").is_file(), "Install full Windows Python including pythonw.exe.")
        add("scheduler", shutil.which("schtasks.exe"), "Enable Windows Task Scheduler.")
    if sys.platform == "darwin":
        add("launchctl", shutil.which("launchctl"), "Use a macOS account with launchd.")
    drive = drive_client(drive_path)
    add("drive_client", drive, "Install Synology Drive Client, or pass --drive-path for a custom location: " + DOWNLOAD)
    enrolled = (state / "profile.json").is_file()
    ours = enrolled and load(state).local_port == local_port and runtime.snapshot(state)["phase"] in ("routing", "ssh_running")
    add("local_port", ours or runtime.local_free(local_port), "Use a spare --local-port for a trial; keep the existing listener running.")
    return dict(schema=SCHEMA, version=__version__, platform=sys.platform, ready=all(i["ok"] for i in checks),
                enrolled=enrolled, local_port=local_port, checks=checks)


def parse_invite(data, role="client", local_port=16690, drive_port=6690):
    fields = {"schema", "host", "host_key", "ssh_port", "bridge_port"}
    if not isinstance(data, dict) or not fields <= data.keys():
        raise ValueError("incomplete invitation")
    version = data["schema"]
    if version == "drive-ssh-relay/v1" and set(data) == fields:
        routes = Routes()
    elif version == "drive-ssh-relay/v2" and set(data) == fields | {"routing"}:
        routes = Routes(**data["routing"])
    else:
        raise ValueError("unrecognized enrollment file; obtain a trusted invitation")
    profile = Profile(role, data["host"], data["host_key"], data["ssh_port"], data["bridge_port"], local_port, drive_port)
    return profile, routes


def read_invite(path):
    if path.stat().st_size > 16384:
        raise ValueError("invitation exceeds 16 KiB")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def setup(state, invitation, local_port=16690, drive_path=None):
    profile, routes = parse_invite(invitation, local_port=local_port)
    inventory = preflight(state, local_port, drive_path)
    if not inventory["ready"]:
        return dict(schema=SCHEMA, status="prerequisites", checks=inventory["checks"])
    public = initialize(state, profile)
    if not (state / "routing.json").exists():  # Preserve user overrides on repeat enrollment.
        save(state, profile, routes)
    return dict(schema=SCHEMA, status="ready_for_authorization", device_public_key=public,
                vps_fingerprint=fingerprint(profile.host_key), mode=settings(state).mode,
                drive_local_port=local_port, next=["authorize_device_on_vps", "service_install", "verify_file_both_directions"])


def describe(state):
    result = dict(schema=SCHEMA, version=__version__, enrolled=(state/"profile.json").is_file())
    if result["enrolled"]:
        profile = load(state)
        result.update(role=profile.role, vps=profile.host, local_port=profile.local_port,
                      routing=asdict(settings(state)), runtime=runtime.snapshot(state))
        if profile.role == "client":
            result["transport"] = transport(state, profile, result["runtime"])
    connection=state/"fleet.json"
    if connection.is_file():
        value=json.loads(connection.read_text(encoding="utf-8"))
        result["fleet"]={"url":value["url"],"name":value["name"],"linked":True}
    elif (state/"fleet-url.txt").is_file():
        result["fleet"]={"url":(state/"fleet-url.txt").read_text(encoding="utf-8").strip(),"linked":False}
    if result["enrolled"]:
        public = state / "identity.pub"
        if public.is_file():
            result["device_public_key"] = public.read_text(encoding="ascii").strip()
    return result
