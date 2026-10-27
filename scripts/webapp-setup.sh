#!/bin/bash

# Prepare stable launchers and their Python environment at install/update.
# Project code is always refreshed; an unchanged environment skips uv sync.
set -e

(($# == 0)) || { echo "usage: webapp-setup.sh" >&2; exit 1; }
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
project=$(cd -- "$here/.." && pwd)
data="$HOME/.local/share/webapp-maker"
runtime="$data/runtime"

for source in "$here/webapp.py" "$project/pyproject.toml" "$project/uv.lock"; do
  [[ -f $source ]] || { echo "Error: missing runtime source: $source" >&2; exit 1; }
done
mkdir -p "$runtime"
cp -- "$here/webapp.py" "$runtime/webapp.py"
cp -- "$project/pyproject.toml" "$runtime/pyproject.toml"
cp -- "$project/uv.lock" "$runtime/uv.lock"

for script in webapp-run webapp-supervisor webapp-install.sh webapp-stop.sh webapp-remove.sh; do
  cp -- "$here/$script" "$data/$script"
  chmod +x "$data/$script"
done
cp -- "$here/webapp-launch.sh" "$data/webapp-launch"
chmod +x "$data/webapp-launch"

signature=$(/usr/bin/python3 - "$runtime" <<'PY'
import hashlib
from pathlib import Path
import sys

root = Path(sys.argv[1])
digest = hashlib.sha256()
for name in ("pyproject.toml", "uv.lock"):
    digest.update(name.encode())
    digest.update((root / name).read_bytes())
digest.update(str(Path(sys.executable).resolve()).encode())
digest.update(sys.version.encode())
print(digest.hexdigest())
PY
)
previous=""
[[ ! -f $runtime/.sync-signature ]] || read -r previous <"$runtime/.sync-signature"
if [[ ! -x $runtime/.venv/bin/python || $signature != "$previous" ]]; then
  # Empty dependencies and the system interpreter make setup work offline.
  UV_PROJECT_ENVIRONMENT="$runtime/.venv" /usr/bin/uv sync --project "$runtime" --locked --offline --no-python-downloads --python /usr/bin/python3
  printf '%s\n' "$signature" >"$runtime/.sync-signature"
fi
printf 'Webapp runtime ready.\n'
