"""Build a deterministic desktop download from the public release tree."""

import argparse
from pathlib import Path
import sys
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[2]
ENTRY_POINTS = (
    "LICENSE",
    "README.md",
    "pyproject.toml",
    "relay.py",
    "install_desktop.py",
    "Install Relay.vbs",
    "Install Relay.command",
    "Open Relay.vbs",
    "Drive Relay.app/Contents/Info.plist",
    "Drive Relay.app/Contents/MacOS/launcher",
)
MODULES = (
    "__init__.py",
    "cli.py",
    "config.py",
    "fleet.py",
    "fleetdb.py",
    "managed.py",
    "observed.py",
    "onboard.py",
    "panel.py",
    "probe.py",
    "routing.py",
    "runtime.py",
    "services.py",
    "fleet.html",
    "fleet.js",
    "panel.html",
    "panel.css",
    "panel.js",
)
DOCS = ("ai.md", "operations.md", "fleet.md", "design-review.md", "DEVELOPMENT_PLAN.md")


def build(target):
    sys.path.insert(0, str(ROOT))
    from drive_relay import __version__ as version

    files = (
        list(ENTRY_POINTS)
        + ["drive_relay/" + name for name in MODULES]
        + ["docs/" + name for name in DOCS]
    )
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = ZipInfo(f"Drive-SSH-Relay-{version}/{name}", date_time=(2026, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (
                0o100755 if name.endswith((".command", "/launcher")) else 0o100644
            ) << 16
            archive.writestr(info, (ROOT / name).read_bytes(), compress_type=ZIP_DEFLATED)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    build(parser.parse_args().target)
