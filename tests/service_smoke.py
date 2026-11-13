"""Exercise service registration with an isolated profile and loopback fixture."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from drive_relay import config, services


def wait(fn, seconds=18):
    limit=time.monotonic()+seconds
    while time.monotonic() < limit:
        try:
            if fn():
                return
        except (ValueError,OSError):
            pass
        time.sleep(.25)
    raise RuntimeError("service lifecycle timed out: " + fn.__name__)


def main():
    with tempfile.TemporaryDirectory(prefix="drive relay smoke ") as tmp, socket.socket() as conflict:
        root=Path(tmp)
        state=root/"endpoint"
        conflict.bind(("127.0.0.1",0))
        conflict.listen()
        port=conflict.getsockname()[1]
        key=root/"testhost"
        subprocess.run([config.executable("ssh-keygen"),"-q","-t","ed25519","-N","","-f",str(key)],check=True,**config.quiet_kwargs())
        config.initialize(state,config.Profile("client","127.0.0.1",key.with_suffix(".pub").read_text(),
                                              ssh_port=port,local_port=port))
        def status():
            return json.loads((state/"status.json").read_text())
        def blocked():
            return status()["detail"]=="local_port_conflict"
        if os.name=="nt":
            child=config.quiet_kwargs()
            # This is the same GUI interpreter and process creation path as the real registration.
            source=("import ctypes,subprocess,sys;from pathlib import Path;"
                    "p=subprocess.run([str(Path(sys.executable).with_name('python.exe')),'-I','-c',"
                    "'import ctypes,sys;sys.exit(bool(ctypes.windll.kernel32.GetConsoleWindow()))'],"
                    "creationflags=subprocess.CREATE_NO_WINDOW);"
                    "sys.exit(1 if ctypes.windll.kernel32.GetConsoleWindow() else p.returncode)")
            subprocess.run([str(Path(sys.executable).with_name("pythonw.exe")),"-I","-c",source],
                           check=True,**child)
        try:
            services.service("install",state)
            wait(blocked)
            pid=status()["supervisor"]
            services.service("install",state)
            wait(blocked)
            assert status()["supervisor"]==pid
            services.service("stop",state)
            services.service("start",state)
            wait(lambda:blocked() and status()["supervisor"]!=pid)
            print("passed: install, collision diagnosed, idempotence, stop and restart",flush=True)
        finally:
            services.service("uninstall",state)
        assert (state/"identity").is_file()
        print("passed: uninstall retained device identity",flush=True)


if __name__=="__main__":
    main()
