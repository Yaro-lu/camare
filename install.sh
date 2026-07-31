#!/bin/sh
set -eu

root_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
	echo "ERROR: python3 is required." >&2
	exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
	echo "ERROR: Python 3.10 or newer is required." >&2
	exit 1
fi

exec python3 "$root_dir/deploy.py" "$@"
