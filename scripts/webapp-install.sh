#!/bin/bash

# Install an ordinary website launcher, or a local-service launcher whose
# short-lived runner resolves a fixed, logged or script-produced URL.

set -e
original_args=("$@")

ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
DESKTOP_DIR="$HOME/.local/share/applications"
DATA_DIR="$HOME/.local/share/webapp-maker"

LAUNCHER=""
RUNNER=""
START_CMD=""
URL_SOURCE="fixed"
URL_REGEX=""
EXTRACT_SCRIPT=""
URL_TIMEOUT="15"
WORKING_DIR=""
AUTO_OPEN="true"
EXTRA_FLAGS=""

fail() { echo "Error: $*" >&2; exit 1; }
need_value() { (($# >= 2)) || fail "$1 needs a value."; }

args=()
while (($#)); do
  case "$1" in
    --launcher)
      need_value "$@"; LAUNCHER=$2; shift 2 ;;
    --runner | --supervisor)
      need_value "$@"; RUNNER=$2; shift 2 ;;
    --start-cmd)
      need_value "$@"; START_CMD=$2; shift 2 ;;
    --url-source)
      need_value "$@"; URL_SOURCE=$2; shift 2 ;;
    --url-regex)
      need_value "$@"; URL_REGEX=$2; shift 2 ;;
    --extract-script)
      need_value "$@"; EXTRACT_SCRIPT=$2; shift 2 ;;
    --url-timeout)
      need_value "$@"; URL_TIMEOUT=$2; shift 2 ;;
    --cwd)
      need_value "$@"; WORKING_DIR=$2; shift 2 ;;
    --auto-open)
      AUTO_OPEN="true"; shift ;;
    --auto-open=*)
      AUTO_OPEN=${1#*=}; shift ;;
    --flags)
      need_value "$@"; EXTRA_FLAGS=$2; shift 2 ;;
    --)
      shift; args+=("$@"); break ;;
    --*)
      fail "unknown option: $1" ;;
    *)
      args+=("$1"); shift ;;
  esac
done
set -- "${args[@]}"

(($# >= 2 && $# <= 3)) || fail 'usage: webapp-install.sh (--launcher PATH | --runner PATH --start-cmd CMD) [options] NAME URL [ICON]'
APP_NAME=$1
APP_URL=$2
ICON_REF=${3:-}

[[ -n $APP_NAME && $APP_NAME != */* ]] || fail "app name must be non-empty and cannot contain '/'."
for value in "$APP_NAME" "$APP_URL" "$START_CMD" "$URL_REGEX" "$EXTRACT_SCRIPT" "$WORKING_DIR" "$EXTRA_FLAGS" "$RUNNER" "$LAUNCHER" "$ICON_REF"; do
  [[ $value != *$'\n'* && $value != *$'\r'* ]] || fail "options must fit on a single line."
done

if [[ -n $START_CMD ]]; then
  [[ -n $RUNNER ]] || fail "--start-cmd needs --runner PATH."
else
  [[ -z $RUNNER ]] || fail "--runner needs --start-cmd CMD."
  [[ -n $LAUNCHER ]] || fail "ordinary websites need --launcher PATH."
  [[ $URL_SOURCE == fixed ]] || fail "--url-source requires --start-cmd CMD."
fi

case $URL_SOURCE in
  fixed)
    [[ -n $APP_URL ]] || fail "fixed URL source needs a URL." ;;
  regex)
    [[ -n $URL_REGEX ]] || fail "regex URL source needs --url-regex." ;;
  script)
    [[ -n $EXTRACT_SCRIPT ]] || fail "script URL source needs --extract-script CMD." ;;
  *)
    fail "--url-source must be fixed, regex or script." ;;
esac
AUTO_OPEN=${AUTO_OPEN,,}
case $AUTO_OPEN in
  true | false) ;;
  *) fail "--auto-open must be true or false." ;;
esac

# Ordinary website input retains the convenient schemeless form. Logged
# URLs and script output are complete HTTP(S) URLs, checked by the runner.
if [[ -n $APP_URL && ! $APP_URL =~ ^[a-zA-Z][a-zA-Z0-9+.-]*: ]]; then
  APP_URL="https://$APP_URL"
fi

# The Unicode fold is shared with the runner. Validate before creating any
# files, so invalid expressions and URLs leave no half-installed launcher.
SAFE_NAME=$(python3 - "$APP_NAME" "$APP_URL" "$URL_SOURCE" "$URL_REGEX" "$URL_TIMEOUT" "$EXTRA_FLAGS" <<'PY'
import math
import re
import shlex
import sys
from urllib.parse import urlsplit

name, url, source, regex, timeout, flags = sys.argv[1:]
safe = re.sub(r"[\W_]+", "-", name.lower(), flags=re.UNICODE).strip("-")
if not safe:
    sys.exit("Error: app name needs at least one letter or digit.")
try:
    browser_flags = shlex.split(flags)
except ValueError:
    sys.exit("Error: --flags has invalid shell quoting.")
reserved = {"--app", "--app-id", "--user-data-dir"}
if any(flag.split("=", 1)[0] in reserved for flag in browser_flags):
    sys.exit("Error: --flags cannot override --app, --app-id or --user-data-dir.")
try:
    seconds = float(timeout)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError
except ValueError:
    sys.exit("Error: --url-timeout must be a positive number of seconds.")
if url:
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or any(c.isspace() for c in url):
            raise ValueError
    except ValueError:
        sys.exit("Error: web app URL must be a complete HTTP(S) URL without whitespace.")
if source == "regex":
    try:
        re.compile(regex)
    except re.error:
        sys.exit("Error: --url-regex is not a valid Python regular expression.")
print(safe)
PY
)

# Desktop launchers need absolute executables. Extract-script is a shell
# command interpreted in cwd at launch time and must remain literal.
[[ -z $RUNNER || $RUNNER == /* ]] || RUNNER="$PWD/$RUNNER"
[[ -z $LAUNCHER || $LAUNCHER == /* ]] || LAUNCHER="$PWD/$LAUNCHER"
if [[ -n $WORKING_DIR ]]; then
  [[ $WORKING_DIR == /* ]] || WORKING_DIR="$PWD/$WORKING_DIR"
  [[ -d $WORKING_DIR ]] || fail "--cwd must name an existing directory."
fi

download_icon() {
  curl -fsSL --max-time 10 -o "$2" "$1" 2>/dev/null &&
    [[ -s $2 && $(file -b --mime-type "$2") == image/* ]]
}

fetch_site_icon() {
  local site_url="$1" dest="$2"
  local origin page icon_url
  origin=$(sed -E 's|^(https?://[^/]+).*|\1|' <<<"$site_url")
  page=$(curl -fsSL --max-time 5 "$site_url" 2>/dev/null | head -c 100000 | tr '\n' ' ')
  icon_url=$(grep -oiE "<link[^>]*rel=[\"'][^\"']*apple-touch-icon[^\"']*[\"'][^>]*>" <<<"$page" |
    grep -oiE "href=[\"'][^\"']+" | head -1 | sed -E "s/^href=[\"']//")
  case $icon_url in
    http://* | https://*) ;;
    //*) icon_url="https:$icon_url" ;;
    /*) icon_url="$origin$icon_url" ;;
    ?*) icon_url="$origin/$icon_url" ;;
  esac
  { [[ -n $icon_url ]] && download_icon "$icon_url" "$dest"; } ||
    download_icon "$origin/apple-touch-icon.png" "$dest" ||
    download_icon "https://www.google.com/s2/favicons?domain=${site_url}&sz=256" "$dest"
}

# Python's existing stdlib owns the lock across one bounded self-reexec.
# The second invocation verifies the inherited descriptor and continues.
python3 - "$SAFE_NAME" "${BASH_SOURCE[0]}" "${original_args[@]}" <<'PYLOCK'
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

ICON_VALUE=$SAFE_NAME
if [[ -z $ICON_REF && -n $START_CMD ]]; then
  ICON_VALUE="web-browser"
else
  mkdir -p "$ICON_DIR"
  if [[ -z $ICON_REF ]]; then
    fetch_site_icon "$APP_URL" "$ICON_DIR/$SAFE_NAME.png" || fail "could not download the site icon."
  elif [[ $ICON_REF =~ ^https?:// ]]; then
    download_icon "$ICON_REF" "$ICON_DIR/$SAFE_NAME.png" || fail "could not download the icon."
  elif [[ -f $ICON_REF ]]; then
    cp -- "$ICON_REF" "$ICON_DIR/$SAFE_NAME.png"
  else
    ICON_VALUE=$ICON_REF
  fi
fi
if [[ -d $HOME/.local/share/icons/hicolor ]] && command -v gtk-update-icon-cache >/dev/null; then
  gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 ||
    echo "warning: icon cache update was unavailable" >&2
fi

if [[ -n $START_CMD ]]; then
  mkdir -p "$DATA_DIR/apps"
  # Flat conf values preserve backslashes and inner quotes exactly; this
  # format is deliberately not sourced as shell or parsed as TOML.
  {
    printf 'name = "%s"\n' "$APP_NAME"
    printf 'start_cmd = "%s"\n' "$START_CMD"
    printf 'url_source = "%s"\n' "$URL_SOURCE"
    printf 'url = "%s"\n' "$APP_URL"
    printf 'url_regex = "%s"\n' "$URL_REGEX"
    printf 'extract_script = "%s"\n' "$EXTRACT_SCRIPT"
    printf 'url_timeout_s = "%s"\n' "$URL_TIMEOUT"
    printf 'cwd = "%s"\n' "$WORKING_DIR"
    printf 'flags = "%s"\n' "$EXTRA_FLAGS"
    printf 'auto_open = "%s"\n' "$AUTO_OPEN"
  } >"$DATA_DIR/apps/$SAFE_NAME.conf"
  chmod 600 "$DATA_DIR/apps/$SAFE_NAME.conf"
fi

desktop_string_escape() {
  local value="$1"
  value=${value//\\/\\\\}
  value=${value//$'\t'/\\t}
  value=${value//$'\r'/\\r}
  value=${value//$'\n'/\\n}
  [[ $value == " "* ]] && value="\\s${value# }"
  printf '%s' "$value"
}

desktop_exec_arg() {
  local escaped
  escaped=$(printf '%s' "$1" |
    sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/`/\\`/g' -e 's/\$/\\$/g' -e 's/%/%%/g')
  printf '"%s"' "$escaped"
}

if [[ -n $START_CMD ]]; then
  EXEC_COMMAND="$(desktop_exec_arg "$RUNNER") $(desktop_exec_arg launch) $(desktop_exec_arg "$SAFE_NAME")"
else
  EXEC_COMMAND="$(desktop_exec_arg "$LAUNCHER") $(desktop_exec_arg "$APP_URL") $(desktop_exec_arg --webapp-name) $(desktop_exec_arg "$SAFE_NAME")"
  # shlex preserves quoted values with spaces and avoids pathname expansion.
  flags_exec=$(python3 - "$EXTRA_FLAGS" <<'PY'
import shlex
import sys

def desktop_arg(value):
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
    return '"' + value + '"'

print(" ".join(desktop_arg(flag) for flag in shlex.split(sys.argv[1])))
PY
)
  [[ -z $flags_exec ]] || EXEC_COMMAND+=" $flags_exec"
fi

mkdir -p "$DESKTOP_DIR"
DESKTOP_FILE="$DESKTOP_DIR/$APP_NAME.desktop"
cat >"$DESKTOP_FILE" <<EOF
[Desktop Entry]
Version=1.0
Name=$(desktop_string_escape "$APP_NAME")
Comment=$(desktop_string_escape "$APP_NAME")
Exec=$(desktop_string_escape "$EXEC_COMMAND")
Terminal=false
Type=Application
Icon=$(desktop_string_escape "$ICON_VALUE")
StartupNotify=true
X-Webapp-Maker=true
EOF
chmod +x "$DESKTOP_FILE"
if command -v update-desktop-database >/dev/null; then
  update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 ||
    echo "warning: desktop database update was unavailable" >&2
fi

# Structured website fields are independent of managed service configurations.
if [[ -z $START_CMD ]]; then
  python3 - "$DATA_DIR" "$SAFE_NAME" "$APP_NAME" "$APP_URL" "$EXTRA_FLAGS" <<'PYENTRY'
import json
import os
from pathlib import Path
import sys
import tempfile
base, safe, name, url, flags = sys.argv[1:]
directory = Path(base) / "entries"
directory.mkdir(mode=0o700, parents=True, exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix=".install-", dir=directory)
try:
    with os.fdopen(fd, "w") as handle:
        json.dump(dict(schema_version=1, safe=safe, name=name, kind="website", url=url, flags=flags), handle, ensure_ascii=False)
        handle.write("\n")
    os.replace(temporary, directory / (safe + ".json"))
finally:
    Path(temporary).unlink(missing_ok=True)
PYENTRY
fi
