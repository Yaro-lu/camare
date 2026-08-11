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

if [ "${1:-}" = "legacy" ]; then
	shift
	echo "WARNING: deploy.py is the legacy installer and will be removed in a future release." >&2
	exec python3 "$root_dir/deploy.py" "$@"
fi

cd "$root_dir"

if [ "$#" -eq 0 ]; then
	if [ -f "$root_dir/camare.ini" ]; then
		set -- apply --config "$root_dir/camare.ini"
	else
		set -- init --config "$root_dir/camare.ini"
	fi
fi

exec python3 -m camarectl "$@"
