#!/bin/sh
# Start the jukebox with its own Python environment (.venv), creating it on first run.
# The browser opens by itself: the setup page the first time, then the admin page.
# Press Ctrl+C to stop the jukebox.
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
    PY=""
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 &&
           "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            PY=$candidate
            break
        fi
    done
    if [ -z "$PY" ]; then
        echo "The jukebox needs Python 3.11 or newer, and it wasn't found."
        if [ "$(uname)" = "Darwin" ]; then
            # The python3 that comes with macOS' developer tools is usually too old (3.9).
            echo "Install it from https://www.python.org/downloads/macos/ (opening it now),"
            echo "then start the jukebox again."
            open "https://www.python.org/downloads/macos/" 2>/dev/null
        else
            echo "Install it with your package manager or from https://www.python.org/downloads/"
        fi
        exit 1
    fi
    echo "Setting up the jukebox, this takes a minute the first time..."
    if ! "$PY" -m venv .venv; then
        echo "Couldn't create the Python environment. On Debian/Ubuntu: sudo apt install python3-venv"
        exit 1
    fi
fi

.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt || exit 1
exec .venv/bin/python run.py
