"""Exchange an identifiable, random file through the user's actual Drive folder."""
import hashlib
import json
import os
from pathlib import Path
import re
import time

PATTERN = re.compile(r"drive-probe-([0-9]{1,8})-([0-9a-f]{64})\.bin")
MAX_BYTES = 16 * 1024 * 1024


def send(folder, mib):
    if not folder.is_dir() or not 1 <= mib <= 16:
        raise ValueError("choose an existing synchronized folder and 1–16 MiB")
    start = time.monotonic()
    payload = os.urandom(mib * 1024 * 1024)
    digest = hashlib.sha256(payload).hexdigest()
    name = f"drive-probe-{len(payload)}-{digest}.bin"
    with (folder/name).open("xb") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    print(json.dumps({"file":name, "bytes":len(payload), "sha256":digest,
                      "local_write_seconds":round(time.monotonic()-start, 3)}))
    return 0


def verify(folder, name, timeout=0, clean=False):
    m = PATTERN.fullmatch(name)
    if not m or not 0 < int(m[1]) <= MAX_BYTES or not 0 <= timeout <= 3600 or not folder.is_dir():
        raise ValueError("invalid probe filename, directory or timeout")
    path = folder / name
    start = time.monotonic()
    reason = "absent_or_incomplete"
    while True:
        info = None
        try:
            if path.is_symlink() or path.resolve().parent != folder.resolve():
                raise ValueError("probe must be a regular file inside the selected folder")
            info = path.stat()
            if path.is_file() and info.st_size == int(m[1]):
                with path.open("rb") as f:
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: f.read(1024*1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() == m[2]:
                    if clean:
                        path.unlink()
                    print(json.dumps({"file":name, "verified":True, "removed":clean,
                                      "observed_wait_seconds":round(time.monotonic()-start, 3)}))
                    return 0
                reason = "different_digest"
        except FileNotFoundError:
            reason = "absent_or_incomplete"
        except OSError as exc:
            attrs = getattr(info, "st_file_attributes", 0)
            if attrs & (0x1000 | 0x40000 | 0x400000):
                reason = "cloud_placeholder_unreadable"
            else:
                reason = "read_error_" + str(getattr(exc, "winerror", None) or exc.errno)
        remaining = start + timeout - time.monotonic()
        if remaining <= 0:
            print(json.dumps({"file":name, "verified":False, "reason":reason}))
            return 2
        time.sleep(min(remaining, 0.5))
