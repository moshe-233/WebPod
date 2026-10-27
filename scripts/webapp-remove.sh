#!/bin/bash

# Remove only launchers addressed to this plugin's stable or source scripts.
# Stop uses the runner's verified identities; there is no raw PID rescue path.
set -e
original_args=("$@")

(($# == 1)) || { echo "usage: webapp-remove NAME" >&2; exit 1; }
name=$1
[[ -n $name && $name != */* ]] || { echo "Not a web app name." >&2; exit 1; }

ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
DESKTOP_DIR="$HOME/.local/share/applications"
DATA_DIR="$HOME/.local/share/webapp-maker"
STATE_DIR="$HOME/.local/state/webapp-maker"
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
desktop_file="$DESKTOP_DIR/$name.desktop"

safe_name=$(python3 - "$desktop_file" "$name" "$DATA_DIR" "$here" <<'PY'
from pathlib import Path
import re
import shlex
import sys

desktop, name, data, scripts = sys.argv[1:]
safe = re.sub(r"[\W_]+", "-", name.lower(), flags=re.UNICODE).strip("-")
if not safe:
    sys.exit("Not a web app name.")
try:
    lines = Path(desktop).read_text().splitlines()
    raw = next(line[5:] for line in lines if line.startswith("Exec="))
    escapes = {"s": " ", "n": "\n", "r": "\r", "t": "\t", "\\": "\\"}
    decoded = re.sub(r"\\([snrt\\])", lambda match: escapes[match[1]], raw)
    args = shlex.split(decoded.replace("%%", "%"))
    executable = Path(args[0])
    allowed_roots = {Path(data).resolve(), Path(scripts).resolve()}
    if executable.parent.resolve() not in allowed_roots:
        raise ValueError
    if executable.name in ("webapp-run", "webapp-supervisor"):
        if args[1:] != ["launch", safe]:
            raise ValueError
    elif executable.name in ("webapp-launch", "webapp-launch.sh"):
        if not args[1:]:
            raise ValueError
    else:
        raise ValueError
except (OSError, StopIteration, ValueError, IndexError):
    sys.exit("Not a web app launcher owned by this plugin.")
print(safe)
PY
)

# Python's existing stdlib owns the lock across one bounded self-reexec.
# The second invocation verifies the inherited descriptor and continues.
python3 - "$safe_name" "${BASH_SOURCE[0]}" "${original_args[@]}" <<'PYLOCK'
import fcntl
import os
from pathlib import Path
import sys

safe, script, *args = sys.argv[1:]
directory = Path.home() / '.local/state/webapp-maker' / safe
directory.mkdir(mode=0o700, parents=True, exist_ok=True)
directory.chmod(0o700)
path = directory / 'operation.lock'
try:
    if os.environ.get('WEBAPP_SCRIPT_LOCK_SCRIPT') == script:
        if os.environ.get('WEBAPP_OPERATION_LOCK_SAFE') != safe:
            raise ValueError
        fd = int(os.environ['WEBAPP_OPERATION_LOCK_FD'])
        held, expected = os.fstat(fd), path.stat()
        if (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError
    else:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if (directory / 'config-edit.pending').exists():
        sys.exit('Error: interrupted configuration save requires recovery; data preserved.')
except (OSError, ValueError, KeyError):
    sys.exit('Error: application operation is busy or its lock is invalid; data preserved.')
if os.environ.get('WEBAPP_SCRIPT_LOCK_SCRIPT') != script:
    os.set_inheritable(fd, True)
    env = dict(os.environ, WEBAPP_OPERATION_LOCK_FD=str(fd),
               WEBAPP_OPERATION_LOCK_SAFE=safe, WEBAPP_SCRIPT_LOCK_SCRIPT=script)
    os.execve('/bin/bash', ['bash', script, *args], env)
PYLOCK
[[ ${WEBAPP_SCRIPT_LOCK_SCRIPT:-} == "${BASH_SOURCE[0]}" ]] || exit 0

if [[ ! -f $here/webapp-stop.sh ]]; then
  echo "Error: stop wrapper is missing; launcher and data preserved." >&2
  exit 1
fi
if ! bash "$here/webapp-stop.sh" "$safe_name" >/dev/null; then
  echo "Error: could not stop '$safe_name'; launcher and data preserved." >&2
  exit 1
fi

rm -f -- "$desktop_file" "$ICON_DIR/$safe_name.png" "$DATA_DIR/apps/$safe_name.conf" "$DATA_DIR/entries/$safe_name.json"
rm -rf -- "$DATA_DIR/profiles/$safe_name" "$STATE_DIR/$safe_name"
if command -v update-desktop-database >/dev/null; then
  update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 ||
    echo "warning: desktop database update was unavailable" >&2
fi
