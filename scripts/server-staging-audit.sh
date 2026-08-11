#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

usage() {
  cat <<'EOF'
Usage:
  server-staging-audit.sh capture --output FILE [--recording-container NAME ...]
  server-staging-audit.sh compare --baseline FILE --output FILE

The audit reads Docker metadata and optional recording mtimes. It never changes
containers, images, networks, volumes, or recording files.
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 2
}

[[ $# -ge 1 ]] || {
  usage >&2
  exit 2
}

action=$1
shift
output=
baseline=
recording_containers=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output)
      [[ $# -ge 2 ]] || die "--output requires a path"
      output=$2
      shift 2
      ;;
    --baseline)
      [[ $# -ge 2 ]] || die "--baseline requires a path"
      baseline=$2
      shift 2
      ;;
    --recording-container)
      [[ $# -ge 2 ]] || die "--recording-container requires a name"
      recording_containers+=("$2")
      shift 2
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

[[ "$action" == "capture" || "$action" == "compare" ]] || \
  die "action must be capture or compare"
[[ -n "$output" ]] || die "--output is required"
if [[ "$action" == "capture" ]]; then
  [[ -z "$baseline" ]] || die "capture does not accept --baseline"
else
  [[ -n "$baseline" ]] || die "compare requires --baseline"
  [[ ${#recording_containers[@]} -eq 0 ]] || \
    die "compare reads recording targets from the baseline"
fi

command -v docker >/dev/null 2>&1 || die "docker is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"

python3 - "$action" "$output" "$baseline" "${recording_containers[@]}" <<'PY'
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile


FORMAT_VERSION = 1
MAX_BASELINE_BYTES = 16 << 20


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"ERROR: {message}")


def docker_json(arguments: list[str]) -> object:
    completed = subprocess.run(
        arguments,
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        fail("Docker state could not be read")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        fail("Docker returned malformed JSON")


def container_documents() -> list[dict[str, object]]:
    completed = subprocess.run(
        ["docker", "ps", "-aq", "--no-trunc"],
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0:
        fail("Docker container inventory could not be read")
    container_ids = sorted(
        line.strip() for line in completed.stdout.splitlines() if line.strip()
    )
    if not container_ids:
        return []
    value = docker_json(["docker", "inspect", *container_ids])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        fail("Docker inspect returned an unexpected document")
    return value


def normalized_ports(value: object) -> dict[str, list[dict[str, str]]]:
    if not isinstance(value, dict):
        return {}
    ports: dict[str, list[dict[str, str]]] = {}
    for name, bindings in value.items():
        if not isinstance(name, str):
            continue
        rows = []
        if isinstance(bindings, list):
            for binding in bindings:
                if isinstance(binding, dict):
                    rows.append(
                        {
                            "HostIp": str(binding.get("HostIp", "")),
                            "HostPort": str(binding.get("HostPort", "")),
                        }
                    )
        ports[name] = sorted(rows, key=lambda row: (row["HostIp"], row["HostPort"]))
    return dict(sorted(ports.items()))


def normalized_networks(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        return {}
    networks: dict[str, dict[str, str]] = {}
    for name, network in value.items():
        if not isinstance(name, str) or not isinstance(network, dict):
            continue
        networks[name] = {
            key: str(network.get(key, ""))
            for key in (
                "NetworkID",
                "EndpointID",
                "Gateway",
                "IPAddress",
                "MacAddress",
            )
        }
    return dict(sorted(networks.items()))


def normalized_mounts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    mounts = []
    for mount in value:
        if not isinstance(mount, dict):
            continue
        mounts.append(
            {
                "Type": str(mount.get("Type", "")),
                "Source": str(mount.get("Source", "")),
                "Destination": str(mount.get("Destination", "")),
                "Mode": str(mount.get("Mode", "")),
                "RW": bool(mount.get("RW", False)),
                "Propagation": str(mount.get("Propagation", "")),
            }
        )
    return sorted(mounts, key=lambda row: (row["Destination"], row["Source"]))


def safe_container(document: dict[str, object]) -> tuple[str, dict[str, object]]:
    config = document.get("Config") if isinstance(document.get("Config"), dict) else {}
    state = document.get("State") if isinstance(document.get("State"), dict) else {}
    network = (
        document.get("NetworkSettings")
        if isinstance(document.get("NetworkSettings"), dict)
        else {}
    )
    labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
    health = state.get("Health") if isinstance(state.get("Health"), dict) else {}
    name = str(document.get("Name", "")).removeprefix("/")
    if not name:
        fail("Docker inspect returned a nameless container")
    return name, {
        "Id": str(document.get("Id", "")),
        "Image": str(document.get("Image", "")),
        "Config.Image": str(config.get("Image", "")),
        "State.Status": str(state.get("Status", "")),
        "State.StartedAt": str(state.get("StartedAt", "")),
        "State.Health.Status": str(health.get("Status", "")),
        "RestartCount": int(document.get("RestartCount", 0)),
        "NetworkSettings.Ports": normalized_ports(network.get("Ports")),
        "NetworkSettings.Networks": normalized_networks(network.get("Networks")),
        "Mounts": normalized_mounts(document.get("Mounts")),
        "Compose.Project": str(labels.get("com.docker.compose.project", "")),
        "Compose.Service": str(labels.get("com.docker.compose.service", "")),
    }


def latest_recording(media_root: Path) -> dict[str, object] | None:
    recordings = media_root / "recordings"
    if not recordings.is_dir() or recordings.is_symlink():
        return None
    latest: tuple[int, str, int] | None = None
    for directory, names, files in os.walk(recordings, followlinks=False):
        names[:] = [name for name in names if not (Path(directory) / name).is_symlink()]
        for name in files:
            if not name.endswith(".mp4"):
                continue
            candidate = Path(directory) / name
            try:
                status = candidate.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(status.st_mode):
                continue
            relative = candidate.relative_to(media_root).as_posix()
            row = (status.st_mtime_ns, relative, status.st_size)
            if latest is None or row[:2] > latest[:2]:
                latest = row
    if latest is None:
        return None
    return {"mtime_ns": latest[0], "path": latest[1], "size": latest[2]}


def snapshot(recording_names: list[str]) -> dict[str, object]:
    documents = container_documents()
    containers = dict(safe_container(document) for document in documents)
    recordings: dict[str, object] = {}
    for name in recording_names:
        if name not in containers:
            fail(f"recording container does not exist: {name}")
        mounts = containers[name]["Mounts"]
        sources = [
            Path(str(mount["Source"]))
            for mount in mounts
            if mount["Destination"] == "/media/frigate" and mount["Type"] == "bind"
        ]
        if len(sources) != 1:
            fail(f"recording container has no unique /media/frigate bind: {name}")
        recording = latest_recording(sources[0])
        if recording is None:
            fail(f"recording container has no readable MP4: {name}")
        recordings[name] = recording
    return {
        "format_version": FORMAT_VERSION,
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "recording_containers": recording_names,
        "containers": dict(sorted(containers.items())),
        "recordings": recordings,
    }


def private_write(path: Path, document: object) -> None:
    raw = path.expanduser().absolute()
    if raw.exists() or raw.is_symlink():
        fail(f"audit output already exists: {raw}")
    parent = raw.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink():
        fail("audit output parent is an unsafe symlink")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{raw.name}.", dir=parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, raw)
    finally:
        temporary.unlink(missing_ok=True)


def read_baseline(path: Path) -> dict[str, object]:
    try:
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode) or path.is_symlink():
            fail("baseline is not a regular private file")
        if status.st_size > MAX_BASELINE_BYTES:
            fail("baseline is too large")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        fail("baseline cannot be read")
    if not isinstance(value, dict) or value.get("format_version") != FORMAT_VERSION:
        fail("baseline format is unsupported")
    return value


def recording_advanced(before: object, after: object) -> bool:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    return (
        after.get("path") != before.get("path")
        or int(after.get("mtime_ns", 0)) > int(before.get("mtime_ns", 0))
        or int(after.get("size", 0)) > int(before.get("size", 0))
    )


action = sys.argv[1]
output_path = Path(sys.argv[2])
baseline_argument = sys.argv[3]
recording_names = list(dict.fromkeys(sys.argv[4:]))
if any(not name or any(character in name for character in "\x00\r\n") for name in recording_names):
    fail("recording container name is invalid")

if action == "capture":
    private_write(output_path, snapshot(recording_names))
    print(f"Audit baseline captured: {output_path}")
    raise SystemExit(0)

baseline = read_baseline(Path(baseline_argument).expanduser().absolute())
baseline_recording_names = baseline.get("recording_containers")
if not isinstance(baseline_recording_names, list) or not all(
    isinstance(name, str) for name in baseline_recording_names
):
    fail("baseline recording inventory is invalid")
current = snapshot(baseline_recording_names)
errors: list[str] = []
if current["containers"] != baseline.get("containers"):
    errors.append("pre-existing container identity or runtime state changed")
before_recordings = baseline.get("recordings")
after_recordings = current.get("recordings")
if not isinstance(before_recordings, dict) or not isinstance(after_recordings, dict):
    errors.append("recording inventory is malformed")
else:
    for name in baseline_recording_names:
        if not recording_advanced(before_recordings.get(name), after_recordings.get(name)):
            errors.append(f"recording did not advance: {name}")

report = {
    "format_version": FORMAT_VERSION,
    "ok": not errors,
    "errors": errors,
    "current": current,
}
private_write(output_path, report)
if errors:
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    raise SystemExit(1)
print("Audit comparison passed")
PY
