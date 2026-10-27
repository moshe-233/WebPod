#!/bin/bash

# Process identity and signal handling belong to the short-lived runner.
set -e
(($# >= 1 && $# <= 2)) || { echo "usage: webapp-stop NAME [GRACE_S]" >&2; exit 1; }
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec "$here/webapp-run" stop "$@"
