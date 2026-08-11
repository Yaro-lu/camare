#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPOSITORY_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
SOURCE_COMPOSE=$SCRIPT_DIR/compose.yaml
AUDIT_SCRIPT=$REPOSITORY_ROOT/scripts/server-staging-audit.sh

usage() {
  cat <<'EOF'
Usage: sudo bash tests/e2e/run.sh [options]

Options:
  --run-id ID                       Explicit ID matching e2e[a-z0-9_-]*.
                                    A random ID is generated when omitted.
  --streams 2|4                     Two streams in one group, or four in two.
  --staging-base DIR                Precreated root:root 0700 directory
                                    (default /var/tmp/camare-e2e).
  --timezone ZONE                   Recording timezone (default Asia/Shanghai).
  --timeout SECONDS                 Compose/apply timeout (default 1200).
  --audit-recording-container NAME  Require this pre-existing recorder to advance.
                                    May be repeated.
  --keep-on-success                 Preserve the private run directory after success.
  -h, --help                        Show this help.

This is an explicit, heavy Linux E2E. It builds Portal images, starts an isolated
Frigate, writes only to its unique staging directory, and never uses a household
LAN address. Failures stop and remove exact E2E Docker objects but retain private
diagnostics and runtime controls under the run directory.
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 2
}

RUN_ID=
STREAM_COUNT=2
STAGING_BASE=${CAMARE_E2E_STAGING_BASE:-/var/tmp/camare-e2e}
RECORDING_TIMEZONE=Asia/Shanghai
COMMAND_TIMEOUT=1200
KEEP_ON_SUCCESS=false
AUDIT_RECORDING_CONTAINERS=()
MEDIAMTX_TAG=bluenviron/mediamtx:1.19.3-ffmpeg

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id)
      [[ $# -ge 2 ]] || die "--run-id requires a value"
      RUN_ID=$2
      shift 2
      ;;
    --streams)
      [[ $# -ge 2 ]] || die "--streams requires 2 or 4"
      STREAM_COUNT=$2
      shift 2
      ;;
    --staging-base)
      [[ $# -ge 2 ]] || die "--staging-base requires a path"
      STAGING_BASE=$2
      shift 2
      ;;
    --timezone)
      [[ $# -ge 2 ]] || die "--timezone requires a value"
      RECORDING_TIMEZONE=$2
      shift 2
      ;;
    --timeout)
      [[ $# -ge 2 ]] || die "--timeout requires seconds"
      COMMAND_TIMEOUT=$2
      shift 2
      ;;
    --audit-recording-container)
      [[ $# -ge 2 ]] || die "--audit-recording-container requires a name"
      AUDIT_RECORDING_CONTAINERS+=("$2")
      shift 2
      ;;
    --keep-on-success)
      KEEP_ON_SUCCESS=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

[[ $(uname -s) == Linux ]] || die "the E2E runner requires Linux"
[[ $(id -u) -eq 0 ]] || die "run this command through sudo"
[[ "$STREAM_COUNT" == 2 || "$STREAM_COUNT" == 4 ]] || \
  die "--streams must be 2 or 4"
[[ "$COMMAND_TIMEOUT" =~ ^[0-9]+$ ]] || die "--timeout must be an integer"
(( COMMAND_TIMEOUT >= 60 && COMMAND_TIMEOUT <= 3600 )) || \
  die "--timeout must be between 60 and 3600 seconds"

for command in docker flock python3 realpath stat; do
  command -v "$command" >/dev/null 2>&1 || die "$command is required"
done

E2E_LOCK_PATH=/run/camare-e2e.lock
[[ ! -L "$E2E_LOCK_PATH" ]] || die "E2E lock path cannot be a symlink"
if [[ -e "$E2E_LOCK_PATH" ]]; then
  [[ -f "$E2E_LOCK_PATH" ]] || die "E2E lock path must be a regular file"
  lock_owner=$(stat -c '%u:%g' -- "$E2E_LOCK_PATH") || \
    die "E2E lock ownership cannot be read"
  [[ "$lock_owner" == "0:0" ]] || die "E2E lock must be owned by root:root"
fi
exec {E2E_LOCK_FD}<>"$E2E_LOCK_PATH" || die "E2E lock cannot be opened"
chmod 0600 "$E2E_LOCK_PATH"
[[ "$(stat -c '%u:%g:%a' -- "$E2E_LOCK_PATH")" == "0:0:600" ]] || \
  die "E2E lock must be root:root mode 0600"
flock -n "$E2E_LOCK_FD" || die "another Camare E2E run is active"

docker info >/dev/null 2>&1 || die "Docker engine is unavailable"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is unavailable"
[[ -f "$SOURCE_COMPOSE" ]] || die "synthetic Compose file is missing"
[[ -f "$AUDIT_SCRIPT" ]] || die "server audit script is missing"
[[ -f "$REPOSITORY_ROOT/install.sh" ]] || die "install.sh is missing"

if [[ -z "$RUN_ID" ]]; then
  RUN_ID="e2e$(date -u +%y%m%d%H%M%S)$(python3 -c 'import secrets; print(secrets.token_hex(2))')"
fi
[[ "$RUN_ID" =~ ^e2e[a-z0-9_-]{0,28}$ ]] || \
  die "run ID must begin with e2e and contain only lowercase safe characters"

raw_staging_base=$STAGING_BASE
[[ "$raw_staging_base" == /* ]] || die "staging base must be absolute"
[[ ! -L "$raw_staging_base" ]] || die "staging base cannot be a symlink"
[[ -e "$raw_staging_base" ]] || die "staging base must already exist"
[[ -d "$raw_staging_base" ]] || die "staging base must be a directory"
STAGING_BASE=$(realpath -e -- "$raw_staging_base") || \
  die "staging base cannot be resolved"
staging_identity=$(stat -c '%u:%g:%a' -- "$STAGING_BASE") || \
  die "staging base ownership cannot be read"
[[ "$staging_identity" == "0:0:700" ]] || \
  die "staging base must be root:root mode 0700"
case "$STAGING_BASE" in
  /|/boot|/dev|/etc|/home|/proc|/root|/run|/sys|/tmp|/usr|/var|/var/tmp)
    die "staging base is too broad"
    ;;
esac
case "$STAGING_BASE/" in
  "$REPOSITORY_ROOT/"*|*/../*)
    die "staging base must stay outside the source checkout"
    ;;
esac
case "$REPOSITORY_ROOT/" in
  "$STAGING_BASE/"*)
    die "staging base cannot contain the source checkout"
    ;;
esac

RUN_ROOT=$STAGING_BASE/$RUN_ID
resolved_run_root=$(realpath -m -- "$RUN_ROOT")
[[ "$resolved_run_root" == "$STAGING_BASE/$RUN_ID" ]] || \
  die "resolved run directory escaped the staging base"
[[ ! -e "$resolved_run_root" && ! -L "$resolved_run_root" ]] || \
  die "run directory already exists; choose a new run ID"

CAMARE_PROJECT=camare_$RUN_ID
SOURCE_PROJECT=camare_e2e_source_$RUN_ID
RUNTIME_ROOT=$RUN_ROOT/runtime
MEDIA_ROOT=$RUN_ROOT/media
INPUT_ROOT=$RUN_ROOT/input
CAMARE_CONFIG=$INPUT_ROOT/camare.ini
SOURCE_ENV=$RUN_ROOT/source.env
AUDIT_BASELINE=$RUN_ROOT/audit-before.json
AUDIT_AFTER=$RUN_ROOT/audit-after.json
DIAGNOSTICS=$RUN_ROOT/diagnostics

project_container_ids() {
  local project=$1
  docker ps -aq --filter "label=com.docker.compose.project=$project"
}

project_network_ids() {
  local project=$1
  docker network ls -q --filter "label=com.docker.compose.project=$project"
}

run_local_image_tags() {
  local image
  while IFS= read -r image; do
    case "$image" in
      local/camare-"$RUN_ID"-portal:src-*|local/camare-"$RUN_ID"-caddy:src-*)
        printf '%s\n' "$image"
        ;;
    esac
  done < <(docker image ls --format '{{.Repository}}:{{.Tag}}')
}

[[ -z "$(project_container_ids "$CAMARE_PROJECT")" ]] || \
  die "Camare E2E project already exists"
[[ -z "$(project_container_ids "$SOURCE_PROJECT")" ]] || \
  die "synthetic E2E project already exists"
[[ -z "$(project_network_ids "$CAMARE_PROJECT")" ]] || \
  die "Camare E2E network already exists"
[[ -z "$(project_network_ids "$SOURCE_PROJECT")" ]] || \
  die "synthetic E2E network already exists"
[[ -z "$(run_local_image_tags)" ]] || \
  die "run-local image already exists; choose a new run ID"

install -d -m 0700 "$RUN_ROOT" "$INPUT_ROOT" "$INPUT_ROOT/secrets" "$DIAGNOSTICS"
install -d -m 0755 "$MEDIA_ROOT"

SUCCESS=false
CLEANUP_FAILED=false
AUDIT_CAPTURED=false
MEDIAMTX_PRESENT_BEFORE=true
MEDIAMTX_IMAGE=
E2E_BIND_IP=
SOURCE_COMPOSE_ARGS=()

remove_exact_project_objects() {
  local project=$1
  local object_id actual_project
  local ids=()
  mapfile -t ids < <(project_container_ids "$project")
  for object_id in "${ids[@]}"; do
    [[ "$object_id" =~ ^[0-9a-f]{12,64}$ ]] || return 1
    actual_project=$(docker inspect \
      --format '{{index .Config.Labels "com.docker.compose.project"}}' \
      "$object_id")
    [[ "$actual_project" == "$project" ]] || return 1
  done
  if (( ${#ids[@]} )); then
    docker rm -f -- "${ids[@]}" >/dev/null
  fi

  mapfile -t ids < <(project_network_ids "$project")
  for object_id in "${ids[@]}"; do
    [[ "$object_id" =~ ^[0-9a-f]{12,64}$ ]] || return 1
    actual_project=$(docker network inspect \
      --format '{{index .Labels "com.docker.compose.project"}}' \
      "$object_id")
    [[ "$actual_project" == "$project" ]] || return 1
  done
  if (( ${#ids[@]} )); then
    docker network rm -- "${ids[@]}" >/dev/null
  fi
}

remove_unique_local_images() {
  local image consumers
  while IFS= read -r image; do
    consumers=$(docker ps -aq --filter "ancestor=$image")
    [[ -z "$consumers" ]] || return 1
    docker image rm -- "$image" >/dev/null
  done < <(run_local_image_tags)
}

write_diagnostics() {
  local original_status=$1
  {
    printf 'run_id=%s\n' "$RUN_ID"
    printf 'original_exit_status=%s\n' "$original_status"
    printf 'camare_project=%s\n' "$CAMARE_PROJECT"
    printf 'source_project=%s\n' "$SOURCE_PROJECT"
    printf 'stream_count=%s\n' "$STREAM_COUNT"
    printf 'bind_ip=%s\n' "${E2E_BIND_IP:-unavailable}"
  } >"$DIAGNOSTICS/summary.txt"
  chmod 0600 "$DIAGNOSTICS/summary.txt"

  docker ps -a \
    --filter "label=com.docker.compose.project=$CAMARE_PROJECT" \
    --format '{{.ID}} {{.Image}} {{.Status}} {{.Labels}}' \
    >"$DIAGNOSTICS/camare-containers.txt" 2>&1 || true
  docker ps -a \
    --filter "label=com.yaro-lu.camare.e2e.run-id=$RUN_ID" \
    --format '{{.ID}} {{.Image}} {{.Status}} {{.Labels}}' \
    >"$DIAGNOSTICS/source-containers.txt" 2>&1 || true
  chmod 0600 "$DIAGNOSTICS/"*.txt

  if (( ${#SOURCE_COMPOSE_ARGS[@]} )); then
    "${SOURCE_COMPOSE_ARGS[@]}" logs --no-color --tail 200 \
      >"$DIAGNOSTICS/source.log" 2>&1 || true
    chmod 0600 "$DIAGNOSTICS/source.log"
  fi
}

on_exit() {
  local status=$1
  local cleanup_status=0
  trap - EXIT INT TERM
  set +e
  [[ "$SUCCESS" == true ]] || status=$(( status == 0 ? 1 : status ))
  write_diagnostics "$status"

  if [[ -f "$RUNTIME_ROOT/manifest.json" && -f "$RUNTIME_ROOT/.env" && \
        -f "$RUNTIME_ROOT/compose.yaml" ]]; then
    docker compose \
      --project-name "$CAMARE_PROJECT" \
      --project-directory "$RUNTIME_ROOT" \
      --env-file "$RUNTIME_ROOT/.env" \
      -f "$RUNTIME_ROOT/compose.yaml" \
      down --remove-orphans >/dev/null 2>&1 || cleanup_status=1
  fi
  remove_exact_project_objects "$CAMARE_PROJECT" || cleanup_status=1

  if (( ${#SOURCE_COMPOSE_ARGS[@]} )); then
    "${SOURCE_COMPOSE_ARGS[@]}" --profile four-streams \
      down --remove-orphans >/dev/null 2>&1 || cleanup_status=1
  fi
  remove_exact_project_objects "$SOURCE_PROJECT" || cleanup_status=1

  remove_unique_local_images || cleanup_status=1
  if [[ "$MEDIAMTX_PRESENT_BEFORE" == false && -n "$MEDIAMTX_IMAGE" ]]; then
    if [[ -z "$(docker ps -aq --filter "ancestor=$MEDIAMTX_IMAGE")" ]]; then
      docker image rm -- "$MEDIAMTX_TAG" >/dev/null 2>&1 || cleanup_status=1
    fi
  fi

  if [[ -n "$(project_container_ids "$CAMARE_PROJECT")" || \
        -n "$(project_container_ids "$SOURCE_PROJECT")" || \
        -n "$(project_network_ids "$CAMARE_PROJECT")" || \
        -n "$(project_network_ids "$SOURCE_PROJECT")" ]]; then
    cleanup_status=1
  fi

  if [[ "$AUDIT_CAPTURED" == true ]]; then
    bash "$AUDIT_SCRIPT" compare \
      --baseline "$AUDIT_BASELINE" \
      --output "$AUDIT_AFTER" || status=1
  fi
  if (( cleanup_status != 0 )); then
    CLEANUP_FAILED=true
    status=1
  fi

  if [[ "$SUCCESS" == true && "$KEEP_ON_SUCCESS" == false && \
        "$CLEANUP_FAILED" == false && $status -eq 0 ]]; then
    resolved_run_root=$(realpath -m -- "$RUN_ROOT")
    if [[ "$resolved_run_root" == "$STAGING_BASE/$RUN_ID" && \
          "$resolved_run_root" != "$STAGING_BASE" && \
          -d "$resolved_run_root" && ! -L "$resolved_run_root" ]]; then
      rm -rf -- "$resolved_run_root"
    else
      printf 'ERROR: refusing unsafe run-directory removal\n' >&2
      status=1
    fi
  else
    printf 'E2E diagnostics preserved at %s\n' "$RUN_ROOT" >&2
  fi
  exit "$status"
}

trap 'on_exit $?' EXIT
trap 'exit 130' INT TERM

audit_arguments=(capture --output "$AUDIT_BASELINE")
for container in "${AUDIT_RECORDING_CONTAINERS[@]}"; do
  audit_arguments+=(--recording-container "$container")
done
bash "$AUDIT_SCRIPT" "${audit_arguments[@]}"
AUDIT_CAPTURED=true

E2E_BIND_IP=$(docker network inspect bridge \
  --format '{{(index .IPAM.Config 0).Gateway}}')
python3 - "$E2E_BIND_IP" <<'PY'
import ipaddress
import socket
import sys

address = ipaddress.ip_address(sys.argv[1])
networks = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12")
)
if address.version != 4 or not any(address in network for network in networks):
    raise SystemExit(
        "Docker bridge gateway must use 10/8 or 172.16/12; "
        "192.168/16 is forbidden"
    )
probe = socket.socket()
try:
    probe.bind((str(address), 0))
finally:
    probe.close()
PY

python3 - "$E2E_BIND_IP" >"$RUN_ROOT/ports.env" <<'PY'
import socket
import sys

bind_ip = sys.argv[1]
specifications = (
    ("SOURCE_PRIMARY_PORT", bind_ip),
    ("SOURCE_SECONDARY_PORT", bind_ip),
    ("LAN_PORT", bind_ip),
    ("FRIGATE_PORT", bind_ip),
    ("PUBLIC_PORT", "127.0.0.1"),
    ("ADMIN_PORT", "127.0.0.1"),
)
sockets = []
ports = set()
for name, host in specifications:
    while True:
        candidate = socket.socket()
        candidate.bind((host, 0))
        port = candidate.getsockname()[1]
        if port not in ports:
            sockets.append(candidate)
            ports.add(port)
            print(f"{name}={port}")
            break
        candidate.close()
PY
chmod 0600 "$RUN_ROOT/ports.env"
SOURCE_PRIMARY_PORT=
SOURCE_SECONDARY_PORT=
LAN_PORT=
FRIGATE_PORT=
PUBLIC_PORT=
ADMIN_PORT=
# shellcheck disable=SC1090
source "$RUN_ROOT/ports.env"
for port in \
  "$SOURCE_PRIMARY_PORT" "$SOURCE_SECONDARY_PORT" "$LAN_PORT" \
  "$FRIGATE_PORT" "$PUBLIC_PORT" "$ADMIN_PORT"; do
  [[ "$port" =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 )) || \
    die "generated port inventory is invalid"
done

if docker image inspect "$MEDIAMTX_TAG" >/dev/null 2>&1; then
  MEDIAMTX_PRESENT_BEFORE=true
else
  MEDIAMTX_PRESENT_BEFORE=false
fi
docker pull "$MEDIAMTX_TAG"
mapfile -t mediamtx_digests < <(
  docker image inspect "$MEDIAMTX_TAG" \
    --format '{{range .RepoDigests}}{{println .}}{{end}}' | \
    grep 'bluenviron/mediamtx@sha256:'
)
MEDIAMTX_IMAGE=${mediamtx_digests[0]:-}
[[ "$MEDIAMTX_IMAGE" =~ ^(docker\.io/)?bluenviron/mediamtx@sha256:[0-9a-f]{64}$ ]] || \
  die "MediaMTX did not resolve to an immutable digest"

cat >"$SOURCE_ENV" <<EOF
E2E_RUN_ID=$RUN_ID
E2E_BIND_IP=$E2E_BIND_IP
SOURCE_PRIMARY_PORT=$SOURCE_PRIMARY_PORT
SOURCE_SECONDARY_PORT=$SOURCE_SECONDARY_PORT
MEDIAMTX_IMAGE=$MEDIAMTX_IMAGE
EOF
chmod 0600 "$SOURCE_ENV"

SOURCE_COMPOSE_ARGS=(
  docker compose
  --project-name "$SOURCE_PROJECT"
  --project-directory "$RUN_ROOT"
  --env-file "$SOURCE_ENV"
  -f "$SOURCE_COMPOSE"
)
if [[ "$STREAM_COUNT" == 4 ]]; then
  SOURCE_COMPOSE_ARGS+=(--profile four-streams)
fi
"${SOURCE_COMPOSE_ARGS[@]}" up -d

wait_for_stream() {
  local service=$1
  local path=$2
  local attempt codec
  for attempt in $(seq 1 45); do
    codec=$("${SOURCE_COMPOSE_ARGS[@]}" exec -T "$service" \
      ffprobe -v error -rtsp_transport tcp -select_streams v:0 \
      -show_entries stream=codec_name \
      -of default=noprint_wrappers=1:nokey=1 \
      "rtsp://127.0.0.1:8554/$path" 2>/dev/null || true)
    [[ "$codec" == h264 ]] && return 0
    sleep 1
  done
  return 1
}

wait_for_stream source-primary cam1 || die "synthetic cam1 did not become H.264"
wait_for_stream source-primary cam2 || die "synthetic cam2 did not become H.264"
if [[ "$STREAM_COUNT" == 4 ]]; then
  wait_for_stream source-secondary cam3 || die "synthetic cam3 did not become H.264"
  wait_for_stream source-secondary cam4 || die "synthetic cam4 did not become H.264"
fi

install -m 0600 /dev/null "$INPUT_ROOT/secrets/synthetic-primary-password"
python3 - "$INPUT_ROOT/secrets/synthetic-primary-password" <<'PY'
from pathlib import Path
import secrets
import sys

Path(sys.argv[1]).write_text(secrets.token_urlsafe(32), encoding="utf-8")
PY
if [[ "$STREAM_COUNT" == 4 ]]; then
  install -m 0600 /dev/null "$INPUT_ROOT/secrets/synthetic-secondary-password"
  python3 - "$INPUT_ROOT/secrets/synthetic-secondary-password" <<'PY'
from pathlib import Path
import secrets
import sys

Path(sys.argv[1]).write_text(secrets.token_urlsafe(32), encoding="utf-8")
PY
fi

groups=synthetic1
if [[ "$STREAM_COUNT" == 4 ]]; then
  groups=synthetic1,synthetic2
fi
cat >"$CAMARE_CONFIG" <<EOF
[camare]
instance_id = $RUN_ID
runtime_dir = $RUNTIME_ROOT
timezone = $RECORDING_TIMEZONE
provider = lan
groups = $groups

[public]
origin =
loopback_port = $PUBLIC_PORT
cloudflare_token_file =

[admin]
origin =
loopback_port = $ADMIN_PORT
tailscale_login =
frigate_bind_ip = $E2E_BIND_IP
frigate_port = $FRIGATE_PORT

[lan]
bind_ip = $E2E_BIND_IP
port = $LAN_PORT

[storage]
media = $MEDIA_ROOT
retention = 1

[hardware]
mode = cpu
render_device =
va_driver =

[group:synthetic1]
name = Synthetic One
host = $E2E_BIND_IP
rtsp_user = synthetic
rtsp_port = $SOURCE_PRIMARY_PORT
password_file = secrets/synthetic-primary-password
record_audio = false
enabled = true
streams = e2ecam1, e2ecam2

[stream:synthetic1:e2ecam1]
name = Synthetic One A
path = /cam1

[stream:synthetic1:e2ecam2]
name = Synthetic One B
path = /cam2
EOF

if [[ "$STREAM_COUNT" == 4 ]]; then
  cat >>"$CAMARE_CONFIG" <<EOF

[group:synthetic2]
name = Synthetic Two
host = $E2E_BIND_IP
rtsp_user = synthetic
rtsp_port = $SOURCE_SECONDARY_PORT
password_file = secrets/synthetic-secondary-password
record_audio = false
enabled = true
streams = e2ecam3, e2ecam4

[stream:synthetic2:e2ecam3]
name = Synthetic Two A
path = /cam3

[stream:synthetic2:e2ecam4]
name = Synthetic Two B
path = /cam4
EOF
fi
chmod 0600 "$CAMARE_CONFIG"

camarectl() {
  bash "$REPOSITORY_ROOT/install.sh" "$@" --config "$CAMARE_CONFIG"
}

camarectl validate
camarectl doctor
camarectl plan
camarectl apply --yes --timeout "$COMMAND_TIMEOUT"
camarectl verify
camarectl status --json

[[ "$(stat -c '%u:%g:%a' "$RUNTIME_ROOT/portal-data")" == "65532:65532:750" ]] || \
  die "Portal state ownership is incorrect"
[[ "$(stat -c '%u:%g:%a' "$RUNTIME_ROOT/secrets/lan-access-key")" == \
   "0:65532:440" ]] || die "LAN access key ownership is incorrect"
camera_secret_paths=("$RUNTIME_ROOT/secrets/camera-password-1")
if [[ "$STREAM_COUNT" == 4 ]]; then
  camera_secret_paths+=("$RUNTIME_ROOT/secrets/camera-password-2")
fi
for camera_secret in "${camera_secret_paths[@]}"; do
  [[ "$(stat -c '%u:%g:%a' "$camera_secret")" == "0:0:600" ]] || \
    die "camera password ownership is incorrect"
done

python3 - "$E2E_BIND_IP" "$LAN_PORT" "$STREAM_COUNT" <<'PY'
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
import sys


base_url = f"http://{sys.argv[1]}:{int(sys.argv[2])}"
expected_cameras = int(sys.argv[3])
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(path: str, timeout: float = 5.0) -> bytes:
    target = base_url + path
    with opener.open(urllib.request.Request(target, method="GET"), timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"unexpected HTTP status {response.status}")
        return response.read(4 << 20)


config = json.loads(request("/api/config"))
cameras = config.get("cameras") if isinstance(config, dict) else None
if not isinstance(cameras, list):
    raise SystemExit("LAN /api/config has no camera list")
camera_ids = [
    camera.get("id")
    for camera in cameras
    if isinstance(camera, dict) and isinstance(camera.get("id"), str)
]
if len(camera_ids) != expected_cameras or len(set(camera_ids)) != expected_cameras:
    raise SystemExit("LAN /api/config camera inventory is incomplete")

selected_camera = None
selected_after = 0
selected_before = 0
for attempt in range(20):
    before = int(time.time()) + 5
    after = before - 95
    for camera in camera_ids:
        query = urllib.parse.urlencode(
            {"camera": camera, "after": after, "before": before}
        )
        recordings = json.loads(request("/api/recordings?" + query))
        if not isinstance(recordings, list):
            raise SystemExit("LAN /api/recordings did not return a list")
        usable = [
            row
            for row in recordings
            if isinstance(row, dict)
            and isinstance(row.get("start_time"), (int, float))
            and isinstance(row.get("end_time"), (int, float))
            and float(row["end_time"]) > float(row["start_time"])
        ]
        if usable:
            first = min(usable, key=lambda row: float(row["start_time"]))
            last = max(usable, key=lambda row: float(row["end_time"]))
            selected_camera = camera
            selected_after = max(after, math.floor(float(first["start_time"])))
            selected_before = min(before, math.ceil(float(last["end_time"])))
            if selected_before <= selected_after:
                selected_before = min(before, selected_after + 1)
            break
    if selected_camera is not None:
        break
    time.sleep(2)
if selected_camera is None:
    raise SystemExit("LAN recordings index did not expose a recent recording")

camera = urllib.parse.quote(selected_camera, safe="")
vod_path = "".join(
    (
        "/media/vod/",
        camera,
        "/start/",
        str(selected_after),
        "/end/",
        str(selected_before),
        "/master.m3u8",
    )
)
last_error = None
for attempt in range(20):
    try:
        body = request(vod_path, timeout=10.0)
        if body.startswith(b"#EXTM3U"):
            break
        last_error = RuntimeError("VOD response is not an HLS manifest")
    except (OSError, urllib.error.HTTPError, RuntimeError) as error:
        last_error = error
    time.sleep(2)
else:
    raise SystemExit(f"LAN VOD playlist did not become ready: {last_error}")
PY

BACKUP_ROOT=$RUN_ROOT/backup-before-mutation
camarectl backup --output "$BACKUP_ROOT"
[[ -z "$(find "$BACKUP_ROOT" -type f -path '*/secrets/*' -print -quit)" ]] || \
  die "backup unexpectedly contains a Docker secret file"
[[ -z "$(find "$BACKUP_ROOT" -type f -path '*/recordings/*' -print -quit)" ]] || \
  die "backup unexpectedly contains recordings"

printf '\n# e2e mutable marker\n' >>"$RUNTIME_ROOT/frigate/config.yml"
camarectl status --json
camarectl restore --archive "$BACKUP_ROOT"
camarectl restore --archive "$BACKUP_ROOT" --apply --yes \
  --timeout "$COMMAND_TIMEOUT"
camarectl verify

camarectl uninstall --apply --yes --timeout "$COMMAND_TIMEOUT"
[[ -z "$(project_container_ids "$CAMARE_PROJECT")" ]] || \
  die "normal uninstall left Camare containers"
[[ -f "$RUNTIME_ROOT/manifest.json" ]] || \
  die "normal uninstall removed the runtime controls"

camarectl apply --yes --timeout "$COMMAND_TIMEOUT"
camarectl verify

MEDIA_SENTINEL=$MEDIA_ROOT/e2e-media-must-survive-purge
touch "$MEDIA_SENTINEL"
camarectl uninstall --purge-data --confirm-instance "$RUN_ID"
camarectl uninstall --apply --yes --purge-data \
  --confirm-instance "$RUN_ID" --timeout "$COMMAND_TIMEOUT"
[[ -f "$MEDIA_SENTINEL" ]] || die "purge removed external recording media"

SUCCESS=true
printf 'E2E completed successfully: run=%s streams=%s\n' "$RUN_ID" "$STREAM_COUNT"
