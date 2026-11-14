"""Describe the owner of the actual Drive endpoint without opening a test connection."""

import ctypes
import ipaddress
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
from .config import LOOPBACK, quiet_kwargs


def windows_rows():
    from ctypes import wintypes as w

    size = w.ULONG()
    api = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
    for _ in range(4):
        buf = ctypes.create_string_buffer(size.value) if size.value else None
        code = api(buf, ctypes.byref(size), False, socket.AF_INET, 5, 0)
        if code == 122:
            continue
        if code or not buf:
            raise OSError("TCP inventory unavailable")
        data = buf.raw

        def addr(v):
            return socket.inet_ntoa(struct.pack("<I", v))

        def port(v):
            return socket.ntohs(v & 65535)

        return [
            (state, addr(la), port(lp), addr(ra), port(rp), pid)
            for state, la, lp, ra, rp, pid in struct.iter_unpack(
                "<6I", data[4 : 4 + 24 * struct.unpack_from("<I", data)[0]]
            )
        ]
    raise OSError("TCP table changed")


def windows_image(pid):
    from ctypes import wintypes as w

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.OpenProcess.restype = w.HANDLE
    k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    k.CloseHandle.argtypes = [w.HANDLE]
    handle = k.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = w.DWORD(len(buf))
        return buf.value if k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else ""
    finally:
        k.CloseHandle(handle)


def classify(rows, profile, runtime, image, legacy):
    owner = next(
        (
            pid
            for state, la, lp, ra, rp, pid in rows
            if state == 2 and la == LOOPBACK and lp == profile.local_port
        ),
        None,
    )
    if not owner:
        return {"kind": "idle", "route": None, "connections": 0}
    peers = {
        pid
        for state, la, lp, ra, rp, pid in rows
        if state == 5 and ra == LOOPBACK and rp == profile.local_port
    }
    apps = {pid: Path(image(pid)).name.lower() for pid in peers}
    count = sum(
        1
        for state, la, lp, ra, rp, pid in rows
        if state == 5
        and ra == LOOPBACK
        and rp == profile.local_port
        and apps.get(pid, "").startswith("cloud-drive-")
    )
    if runtime.get("supervisor") == owner and runtime.get("phase") in ("routing", "ssh_running"):
        return {"kind": "managed", "route": runtime.get("route"), "connections": count}
    try:
        target = str(ipaddress.IPv4Address(profile.host))
    except ValueError:
        target = None
    matches = target and any(
        pid == owner and state == 5 and ra == target and rp == profile.ssh_port
        for state, la, lp, ra, rp, pid in rows
    )
    exe = Path(image(owner))
    system_ssh = Path(os.environ.get("WINDIR", "C:/Windows")) / "System32/OpenSSH/ssh.exe"
    if legacy and matches and os.path.normcase(str(exe)) == os.path.normcase(str(system_ssh)):
        return {"kind": "legacy", "route": "vps", "connections": count}
    return {"kind": "other", "route": None, "connections": count}


def transport(state, profile, runtime):
    if sys.platform == "win32":
        try:
            return classify(
                windows_rows(), profile, runtime, windows_image, (state / "legacy-client.json").is_file()
            )
        except (OSError, ValueError, struct.error):
            pass
    if runtime.get("phase") in ("routing", "ssh_running"):
        return {
            "kind": "managed",
            "route": runtime.get("route"),
            "connections": runtime.get("connections"),
        }
    from .runtime import local_free

    return {
        "kind": "idle" if local_free(profile.local_port) else "other",
        "route": None,
        "connections": None,
    }
