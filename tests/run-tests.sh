#!/bin/bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python="$root/.venv/bin/python"
[[ -x $python ]] || { echo 'Prepare source environment with uv sync first.' >&2; exit 1; }
for script in "$root"/scripts/*.sh "$root"/scripts/webapp-run "$root"/scripts/webapp-supervisor; do
  bash -n "$script"
done
"$python" -B "$root/tests/test-light-launcher.py"
"$python" -B "$root/tests/test-install.py"
"$python" -B "$root/tests/test-config.py"
"$python" -B "$root/tests/check-ui.py"
"$python" - "$root" <<'CHECK'
import ast
from pathlib import Path
import sys
import tomllib
root = Path(sys.argv[1])
ast.parse((root / 'scripts/webapp.py').read_text(), feature_version=(3, 9))
for name in ('plugin.toml', 'pyproject.toml', 'uv.lock'):
    tomllib.loads((root / name).read_text())
print('PASS: Python 3.9 grammar, TOML and Bash syntax')
CHECK
