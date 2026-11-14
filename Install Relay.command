#!/bin/sh
set -eu
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
    echo 'Drive Relay requires Python 3.9 or later. Install Python from https://www.python.org/downloads/macos/ and open this installer again.'
    exit 2
fi
exec python3 ./install_desktop.py --open
