"""Foreground supervisor used by all service managers."""
import contextlib
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import signal
import socket
import subprocess
import threading
import time

from .config import LOOPBACK, atomic, load, quiet_kwargs, ssh_args


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextlib.contextmanager
def exclusive(path):
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as e:
                raise ValueError("another instance is using this profile") from e
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise ValueError("another instance is using this profile") from e
        yield  # Closing the handle releases the lock, including after process termination.


def child_lifetime():
    """On Windows a scheduler stop kills the supervisor and its SSH child together."""
    if os.name != "nt":
        return None
    from ctypes import wintypes as w
    class Limits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64), ("flags", w.DWORD),
                    ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t), ("active", w.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("schedule", w.DWORD)]
    class Extended(ctypes.Structure):
        _fields_ = [("basic", Limits), ("io", ctypes.c_uint64 * 6), ("memory", ctypes.c_size_t * 4)]
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    api.CreateJobObjectW.restype = w.HANDLE
    api.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    api.GetCurrentProcess.restype = w.HANDLE
    job = api.CreateJobObjectW(None, None)
    opts = Extended()
    opts.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not job or not api.SetInformationJobObject(job, 9, ctypes.byref(opts), ctypes.sizeof(opts)) or not api.AssignProcessToJobObject(job, api.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    return job  # Owned by this process until exit, not inherited by SSH.


def tcp(host, port, timeout=2):
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def local_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name == "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind((LOOPBACK, port))
            return True
        except OSError:
            return False


def category(message):
    lower = message.lower()
    for token, label in (("host key verification failed", "host_key_mismatch"),
                         ("identification has changed", "host_key_mismatch"),
                         ("permission denied", "key_rejected"), ("bad permissions", "key_permissions"),
                         ("remote port forwarding failed", "bridge_unavailable"),
                         ("address already in use", "local_port_conflict"),
                         ("connect failed", "forward_destination_failed"),
                         ("could not resolve", "dns_failure"), ("refused", "connection_refused"),
                         ("timed out", "timeout"), ("broken pipe", "connection_lost"),
                         ("connection reset", "connection_lost")):
        if token in lower:
            return label
    return "ssh_exit"


class Report:
    def __init__(self, state):
        self.state = state
        self.previous = None

    def update(self, phase, detail="", pid=None, retry=0):
        payload = dict(time=stamp(), epoch=time.time(), supervisor=os.getpid(), ssh_pid=pid,
                       phase=phase, detail=detail, retry_in_seconds=retry)
        atomic(self.state/"status.json", (json.dumps(payload) + "\n").encode())
        signature = (phase, detail)
        if signature != self.previous:
            log = self.state/"events.log"
            if log.exists() and log.stat().st_size > 256 * 1024:
                log.replace(self.state/"events.previous.log")
            with log.open("a", encoding="utf-8") as f:
                f.write(f"{stamp()} {phase} {detail}\n")
            self.previous = signature


def drain(stream, latest, notice):
    try:
        for line in stream:
            label = category(line.decode(errors="replace"))
            if label != "ssh_exit":
                latest[0] = label
            if label == "forward_destination_failed":
                notice.set()
    finally:
        stream.close()


def run(state):
    profile = load(state)
    with exclusive(state/"run.lock"):
        job = child_lifetime()
        stop = threading.Event()
        request = state / "stop.request"
        request.unlink(missing_ok=True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        report = Report(state)
        delay = 2
        while not stop.is_set() and not request.exists():
            if profile.role == "client" and not local_free(profile.local_port):
                report.update("blocked", "local_port_conflict", retry=5)
                stop.wait(5)
                continue
            started = time.monotonic()
            latest = ["ssh_exit"]
            notice = threading.Event()
            child = None
            reader = None
            try:
                child = subprocess.Popen(ssh_args(state, profile), stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **quiet_kwargs())
                reader = threading.Thread(target=drain, args=(child.stderr, latest, notice), daemon=True)
                reader.start()
                # An SSH process alone cannot prove that Drive answered at the far end.
                heartbeat = 0.0
                while child.poll() is None:
                    if time.monotonic() >= heartbeat:
                        report.update("ssh_running", latest[0] if notice.is_set() else "", child.pid)
                        notice.clear()
                        heartbeat = time.monotonic() + 5
                    if stop.wait(0.5) or request.exists():
                        stop.set()
                        break
            except OSError:
                latest[0] = "ssh_start_failed"
            finally:
                if child and child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                if reader:
                    reader.join(timeout=2)
            if not stop.is_set():
                if time.monotonic() - started >= 60:
                    delay = 2
                wait = round(random.uniform(delay * 0.8, delay), 1)
                report.update("retrying", latest[0], retry=wait)
                stop.wait(wait)
                delay = min(delay * 2, 60)
        request.unlink(missing_ok=True)
        report.update("stopped")
    return 0


def doctor(state):
    p = load(state)
    try:
        status = json.loads((state/"status.json").read_text(encoding="utf-8"))
        fresh = 0 <= time.time() - float(status["epoch"]) <= 12
    except (OSError, ValueError, KeyError):
        status, fresh = {}, False
    occupied = False
    try:
        with exclusive(state/"run.lock"):
            pass
    except ValueError:
        occupied = True
    active = occupied and fresh and status.get("phase") == "ssh_running"
    target = p.local_port if p.role == "client" else p.drive_port
    checks = {"vps_tcp_reachable": tcp(p.host, p.ssh_port),
              "profile_supervisor_active": active,
              "local_forward_tcp" if p.role == "client" else "drive_server_tcp": tcp(LOOPBACK, target)}
    # On a client, connecting to -L only tests accept(). On the NAS, connecting
    # to 6690 tests the listener. A completed end-to-end file probe is separate.
    detail = status.get("detail") if fresh and occupied else "supervisor_not_running_or_stale"
    result = {"role": p.role, "checks": checks, "phase": status.get("phase") if fresh and occupied else "inactive",
              "detail": detail, "file_transfer": "use probe send/ack"}
    print(json.dumps(result, indent=2))
    return 0 if all(checks.values()) and not detail else 2
