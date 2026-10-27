#!/bin/bash

# Launch a URL as a web-app window (no tabs, no URL bar).
# Only chromium-family browsers have an --app mode; when the default browser
# is not one of them (e.g. Firefox), the first installed chromium-family
# browser is used instead. Extra arguments are passed to the browser as-is.
#
# Webapp-aware callers may pass --webapp-name=<safe> (space form works too)
# so the app gets its own chromium profile injected as --user-data-dir, and
# --webapp-env=<file> to source an environment file before the browser
# starts; that file may override WEBAPP_URL (and export anything else).

set -e

DATA_DIR="$HOME/.local/share/webapp-maker"

WEBAPP_NAME=""
WEBAPP_ENV=""
url=""
flags=()
while (($#)); do
  case "$1" in
    --webapp-name=*)
      WEBAPP_NAME=${1#*=}
      shift
      ;;
    --webapp-name)
      WEBAPP_NAME=${2:?--webapp-name needs a value}
      shift 2
      ;;
    --webapp-env=*)
      WEBAPP_ENV=${1#*=}
      shift
      ;;
    --webapp-env)
      WEBAPP_ENV=${2:?--webapp-env needs a value}
      shift 2
      ;;
    *)
      # First bare argument is the URL (upstream calling convention);
      # everything after it goes to the browser.
      if [[ -z $url ]]; then
        url=$1
      else
        flags+=("$1")
      fi
      shift
      ;;
  esac
done

if [[ -z $url ]]; then
  echo "usage: webapp-launch <url> [browser flags...] [--webapp-name <safe>] [--webapp-env <file>]" >&2
  exit 1
fi

# Sourced in this shell on purpose: the env file can export anything the
# browser or the site needs, and WEBAPP_URL set there replaces the URL.
WEBAPP_URL=$url
if [[ -n $WEBAPP_ENV ]]; then
  if [[ -f $WEBAPP_ENV ]]; then
    # shellcheck disable=SC1090
    source "$WEBAPP_ENV" || echo "warning: sourcing $WEBAPP_ENV failed" >&2
  else
    echo "warning: env file not found: $WEBAPP_ENV" >&2
  fi
fi
url=${WEBAPP_URL:-$url}

# Per-app profile: chromium keeps cookies and site data per --user-data-dir,
# so each web app owns its session. Injected only when the caller did not
# choose a profile itself.
if [[ -n $WEBAPP_NAME ]]; then
  has_profile=0
  for flag in "${flags[@]}"; do
    case $flag in
      --user-data-dir=* | --user-data-dir) has_profile=1 ;;
    esac
  done
  if ((has_profile == 0)); then
    flags+=("--user-data-dir=$DATA_DIR/profiles/$WEBAPP_NAME")
  fi
fi

browser=$(xdg-settings get default-web-browser 2>/dev/null || true)
case $browser in
chromium* | chrome* | google-chrome* | brave* | microsoft-edge* | opera* | vivaldi* | helium*) ;;
*) browser="" ;;
esac

exec_bin=""
if [[ -n $browser ]]; then
  exec_bin=$(sed -n 's/^Exec=\([^ ]*\).*/\1/p' \
    "$HOME/.local/share/applications/$browser" "/usr/share/applications/$browser" 2>/dev/null | head -1)
fi
if [[ -z $exec_bin ]]; then
  for candidate in chromium google-chrome-stable google-chrome brave vivaldi microsoft-edge-stable; do
    if command -v "$candidate" >/dev/null; then
      exec_bin=$candidate
      break
    fi
  done
fi
if [[ -z $exec_bin ]]; then
  command -v notify-send >/dev/null && notify-send "Web app" "No chromium-family browser found for app windows"
  exit 1
fi

exec setsid "$exec_bin" --app="$url" "${flags[@]}"
