#!/bin/sh
set -eu

[ -f .env ] || {
  echo "ERROR: .env is missing" >&2
  exit 1
}

set -a
# shellcheck disable=SC1091
. ./.env
set +a

media_root="${FRIGATE_MEDIA_PATH:?set FRIGATE_MEDIA_PATH in .env}"
admin_ip="${FRIGATE_ADMIN_BIND_IP:-127.0.0.1}"
admin_port="${FRIGATE_ADMIN_PORT:-8971}"

docker inspect --format \
  'container={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{end}} restarts={{.RestartCount}}' \
  frigate

docker exec frigate sh -c \
  'wget -qO- http://127.0.0.1:1984/api/streams' |
  python3 -c '
import json
import sys

streams = json.load(sys.stdin)
for name, state in sorted(streams.items()):
    print(
        "{}: producers={}, consumers={}".format(
            name,
            len(state.get("producers", [])),
            len(state.get("consumers", [])),
        )
    )
'

for camera in fixed_view ptz_view; do
  count="$(find "$media_root/recordings" -path "*/$camera/*.mp4" -type f 2>/dev/null | wc -l)"
  bytes="$(
    find "$media_root/recordings" -path "*/$camera/*.mp4" -type f -printf '%s\n' 2>/dev/null |
      awk '{total += $1} END {printf "%d", total}'
  )"
  printf '%s: files=%s bytes=%s\n' "$camera" "$count" "$bytes"
done

curl -kfsS -o /dev/null -w 'ui_https_status=%{http_code}\n' \
  "https://${admin_ip}:${admin_port}/"
df -h "$media_root"
