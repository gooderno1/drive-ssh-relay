"""Install the unpacked client for the current user and create its application shortcut."""

import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
from drive_relay import __version__
from drive_relay.config import atomic, quiet_kwargs
from drive_relay.services import package


def install():
    if sys.platform == "win32":
        root = Path(os.environ["LOCALAPPDATA"]) / "Programs/DriveSSHRelay" / __version__
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support/DriveSSHRelay/versions" / __version__
    else:
        raise ValueError("Use the CLI on Linux.")
    root.mkdir(parents=True, exist_ok=True)
    archive = package(root)
    if sys.platform == "win32":
        pw = Path(sys.executable).with_name("pythonw.exe")
        if not pw.is_file():
            raise ValueError("Windows Python must include pythonw.exe")
        ps = Path(os.environ["WINDIR"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        script = "$ErrorActionPreference='Stop'; $p=Join-Path ([Environment]::GetFolderPath('Programs')) 'Drive SSH Relay.lnk'; $s=(New-Object -ComObject WScript.Shell).CreateShortcut($p); $s.TargetPath=$env:RELAY_PYTHON; $s.Arguments=$env:RELAY_ARGS; $s.WorkingDirectory=$env:RELAY_DIR; $s.Save()"
        subprocess.run(
            [str(ps), "-NoProfile", "-NonInteractive", "-Command", script],
            env=dict(
                os.environ,
                RELAY_PYTHON=str(pw),
                RELAY_ARGS=subprocess.list2cmdline(["-I", "-B", str(archive), "panel", "--open"]),
                RELAY_DIR=str(root),
            ),
            check=True,
            **quiet_kwargs(),
        )
    else:
        app = Path.home() / "Applications/Drive Relay.app/Contents"
        (app / "MacOS").mkdir(parents=True, exist_ok=True)
        import shlex

        atomic(
            app / "MacOS/launcher",
            (
                "#!/bin/sh\nexec "
                + shlex.join([sys.executable, "-I", "-B", str(archive), "panel", "--open"])
                + "\n"
            ).encode(),
            0o755,
        )
        atomic(
            app / "Info.plist",
            plistlib.dumps(
                dict(
                    CFBundleIdentifier="org.drive-relay.desktop",
                    CFBundleName="Drive Relay",
                    CFBundleExecutable="launcher",
                    CFBundlePackageType="APPL",
                )
            ),
            0o644,
        )
    return archive


if __name__ == "__main__":
    bundle = install()
    if "--open" in sys.argv:
        binary = (
            Path(sys.executable).with_name("pythonw.exe") if os.name == "nt" else Path(sys.executable)
        )
        subprocess.Popen([str(binary), "-I", "-B", str(bundle), "panel", "--open"], **quiet_kwargs())
    else:
        print(bundle)
