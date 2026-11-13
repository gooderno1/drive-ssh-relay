"""Register a per-user service without keeping a checkout at a fixed location."""
import csv
import hashlib
import io
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile

from .config import atomic, load, quiet_kwargs
from .runtime import exclusive

TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def command(args, check=True):
    return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=check, **quiet_kwargs())


def package(state):
    archive = getattr(sys.modules.get(__package__), "__loader__", None)
    if getattr(archive, "archive", None):
        data = Path(archive.archive).read_bytes()
    else:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            sources = {"__main__.py": b"from drive_relay.cli import main\nraise SystemExit(main())\n"}
            for f in sorted(Path(__file__).parent.glob("*.py")):
                sources["drive_relay/" + f.name] = f.read_bytes()
            for name, content in sources.items():
                z.writestr(zipfile.ZipInfo(name), content, compress_type=zipfile.ZIP_DEFLATED)
        data = buf.getvalue()
    binary = state / ("runtime-" + hashlib.sha256(data).hexdigest()[:16] + ".pyz")
    if not binary.exists() or binary.read_bytes() != data:
        atomic(binary, data)
    return binary


def identifier(state):
    return "drive-ssh-relay-" + hashlib.sha256(os.fsencode(os.path.normcase(str(state.resolve())))).hexdigest()[:14]


def windows_xml(state, binary, sid):
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    name = identifier(state)
    root = ET.Element("Task", version="1.2", xmlns=TASK_NS)
    def tag(parent, key, value=None, **attrs):
        node = ET.SubElement(parent, key, attrs)
        node.text = value
        return node
    reg = tag(root, "RegistrationInfo")
    tag(reg, "Description", name)
    trig = tag(tag(root, "Triggers"), "LogonTrigger")
    tag(trig, "Enabled", "true")
    tag(trig, "UserId", sid)
    pri = tag(tag(root, "Principals"), "Principal", id="Owner")
    tag(pri, "UserId", sid)
    tag(pri, "LogonType", "InteractiveToken")
    tag(pri, "RunLevel", "LeastPrivilege")
    settings = tag(root, "Settings")
    for key, value in (("MultipleInstancesPolicy","IgnoreNew"), ("DisallowStartIfOnBatteries","false"),
                       ("StopIfGoingOnBatteries","false"), ("StartWhenAvailable","true"),
                       ("Hidden","true"), ("ExecutionTimeLimit","PT0S")):
        tag(settings, key, value)
    restart = tag(settings, "RestartOnFailure")
    tag(restart, "Interval", "PT1M")
    tag(restart, "Count", "5")
    action = tag(tag(root, "Actions", Context="Owner"), "Exec")
    tag(action, "Command", str(pythonw))
    tag(action, "Arguments", subprocess.list2cmdline(["-I", "-B", str(binary), "--state", str(state), "run"]))
    return ET.tostring(root, encoding="utf-16", xml_declaration=True)


def win_service(action, state, binary):
    name = identifier(state)
    sch = str(Path(os.environ["WINDIR"]) / "System32" / "schtasks.exe")
    query = command([sch, "/Query", "/TN", name, "/XML"], check=False)
    previous = None
    if query.returncode == 0:
        raw = query.stdout
        if raw.startswith(b"\xff\xfe") or b"\x00" in raw[:70]:
            raw = raw.decode("utf-16").strip()
        else:
            raw = raw.decode("utf-8-sig").strip()
        previous = ET.fromstring(raw)
        if previous.findtext(".//{*}Description") != name:
            raise ValueError("scheduled task name is owned by another application")
    def stop_task():
        command([sch, "/End", "/TN", name], check=False)
        # /End is asynchronous. A new /Run while IgnoreNew is still active can
        # report success and do nothing. Query the numeric scheduler state.
        ps = Path(os.environ["WINDIR"])/"System32"/"WindowsPowerShell"/"v1.0"/"powershell.exe"
        script = ("$ErrorActionPreference='Stop';$s=New-Object -ComObject Schedule.Service;"
                  "$s.Connect();$folder=$s.GetFolder('\\');"
                  f"for($n=0;$n -lt 80;$n++){{$t=$folder.GetTask('{name}');"
                  "if([int]$t.State -notin @(2,4)){exit 0};Start-Sleep -Milliseconds 150};exit 2")
        command([str(ps), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script])
        for _ in range(40):
            try:
                with exclusive(state/"run.lock"):
                    return
            except ValueError:
                time.sleep(.1)
        raise ValueError("scheduled supervisor still owns the profile lock")
    if action == "uninstall":
        if previous is not None:
            stop_task()
            command([sch, "/Delete", "/TN", name, "/F"])
        return
    if action in ("start", "stop"):
        if action == "stop":
            stop_task()
        else:
            command([sch, "/Run", "/TN", name])
        return
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise ValueError("pythonw.exe is required for windowless startup")
    who = command(["whoami.exe", "/user", "/fo", "csv", "/nh"]).stdout.decode(errors="replace")
    sid = next(csv.reader([who.strip()]))[-1].strip()
    if not sid.startswith("S-1-"):
        raise ValueError("cannot identify current Windows SID")
    data = windows_xml(state, binary, sid)
    new = ET.fromstring(data)
    if previous is None or any(previous.findtext(".//{*}" + k) != new.findtext(".//{*}" + k)
                               for k in ("Command", "Arguments", "UserId")):
        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp)/"task.xml"
            xml.write_bytes(data)
            if previous is not None:
                stop_task()
            command([sch, "/Create", "/TN", name, "/XML", str(xml)] + (["/F"] if previous is not None else []))
    command([sch, "/Run", "/TN", name])


def mac_service(action, state, binary):
    label = "org." + identifier(state)
    domain = f"gui/{os.getuid()}"
    target = domain + "/" + label
    plist = Path.home()/"Library"/"LaunchAgents"/(label + ".plist")
    if plist.exists():
        existing = plistlib.loads(plist.read_bytes())
        if existing.get("Label") != label or str(state) not in existing.get("ProgramArguments", []):
            raise ValueError("launch agent name collision")
    if action == "stop" or action == "uninstall":
        if command(["launchctl", "print", target], check=False).returncode == 0:
            command(["launchctl", "bootout", target])
            for _ in range(80):
                if command(["launchctl","print",target],check=False).returncode:
                    try:
                        with exclusive(state/"run.lock"):
                            break
                    except ValueError:
                        pass
                time.sleep(.15)
            else:
                raise ValueError("launchd has not finished unloading this profile")
        if action == "uninstall":
            plist.unlink(missing_ok=True)
        return
    if action == "install":
        data = plistlib.dumps(dict(Label=label,
            ProgramArguments=[sys.executable,"-I","-B",str(binary),"--state",str(state),"run"],
            RunAtLoad=True, KeepAlive={"SuccessfulExit":False}, ThrottleInterval=10))
        if plist.exists() and plist.read_bytes() != data:
            mac_service("stop", state, binary)
        plist.parent.mkdir(parents=True, exist_ok=True)
        atomic(plist, data)
    if command(["launchctl","print",target],check=False).returncode:
        command(["launchctl", "bootstrap", domain, str(plist)])
    else:
        command(["launchctl", "kickstart", target])


def dsm_service(action, state, binary):
    hook = state / "start-at-boot.sh"
    marker = "# drive-ssh-relay " + identifier(state)
    if hook.exists() and marker not in hook.read_text().splitlines()[:2]:
        raise ValueError("boot script name collision")
    if action == "install":
        args = [sys.executable, "-I", "-B", str(binary), "--state", str(state), "run"]
        script = "#!/bin/sh\n" + marker + "\nnohup " + shlex.join(args) + " </dev/null >/dev/null 2>&1 &\n"
        changed = hook.exists() and hook.read_text() != script
        if changed:
            dsm_service("stop", state, binary)
        atomic(hook, script.encode(), 0o700)
        print("DSM Task Scheduler / Triggered Task / Boot-up, run as the profile owner:\n" +
              "test -x " + shlex.quote(str(hook)) + " && " + shlex.quote(str(hook)))
        dsm_service("start", state, binary)
    elif action == "start":
        if not hook.is_file():
            raise ValueError("install the service first")
        try:
            with exclusive(state/"run.lock"):
                pass
        except ValueError:
            return
        command([str(hook)])
    else:
        try:
            with exclusive(state/"run.lock"):
                pass
        except ValueError:
            atomic(state/"stop.request", b"stop\n")
            for _ in range(75):
                time.sleep(1)
                try:
                    with exclusive(state/"run.lock"):
                        break
                except ValueError:
                    continue
            else:
                raise ValueError("supervisor did not stop within 75 seconds")
        if action == "uninstall":
            hook.unlink(missing_ok=True)
            print("Remove the registered boot task from DSM Task Scheduler.")


def service(action, state):
    load(state)
    with exclusive(state/"service.lock"):
        binary = package(state) if action == "install" else None
        if os.name == "nt":
            win_service(action, state, binary)
        elif sys.platform == "darwin":
            mac_service(action, state, binary)
        else:
            dsm_service(action, state, binary)
    print(f"{action}: {identifier(state)}")
