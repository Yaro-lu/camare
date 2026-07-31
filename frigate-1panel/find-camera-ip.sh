#!/usr/bin/env bash
set -euo pipefail

compose_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
env_file="${compose_dir}/.env"

read_env() {
  local key="$1"
  awk -F= -v wanted="${key}" '$1 == wanted { sub(/^[^=]*=/, ""); print; exit }' "${env_file}"
}

current_ip="$(read_env FRIGATE_CAMERA_HOST)"
camera_mac="$(read_env CAMERA_MAC | tr '[:upper:]' '[:lower:]')"
camera_subnet="$(read_env CAMERA_SUBNET)"

if [[ -z "${current_ip}" || -z "${camera_mac}" || -z "${camera_subnet}" ]]; then
  printf 'FRIGATE_CAMERA_HOST, CAMERA_MAC and CAMERA_SUBNET are required in .env.\n' >&2
  exit 2
fi

if [[ "${camera_mac}" == "00:11:22:33:44:55" ]]; then
  printf 'Replace the example CAMERA_MAC value before running this helper.\n' >&2
  exit 2
fi

if timeout 3 bash -c "</dev/tcp/${current_ip}/554" 2>/dev/null; then
  printf 'Camera RTSP is reachable at %s; no change needed.\n' "${current_ip}"
  exit 0
fi

printf 'Current camera IP %s is unreachable; scanning %s.0/24 by MAC %s...\n' \
  "${current_ip}" "${camera_subnet}" "${camera_mac}"

for start in $(seq 1 32 253); do
  end=$((start + 31))
  if (( end > 254 )); then
    end=254
  fi
  for host in $(seq "${start}" "${end}"); do
    ping -c 1 -W 1 "${camera_subnet}.${host}" >/dev/null 2>&1 &
  done
  wait || true
done

new_ip="$(
  ip neigh show |
    awk -v wanted="${camera_mac}" '
      tolower($5) == wanted && $NF != "FAILED" { print $1; exit }
    '
)"

if [[ -z "${new_ip}" ]]; then
  printf 'Camera MAC %s was not found. No configuration was changed.\n' "${camera_mac}" >&2
  exit 1
fi

if ! [[ "${new_ip}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
  printf 'Refusing unexpected IP value: %s\n' "${new_ip}" >&2
  exit 1
fi

cp -a -- "${env_file}" "${env_file}.bak"
sed -i -E "s/^FRIGATE_CAMERA_HOST=.*/FRIGATE_CAMERA_HOST=${new_ip}/" "${env_file}"

printf 'Camera moved from %s to %s. Recreating only the Frigate service...\n' \
  "${current_ip}" "${new_ip}"
docker compose --project-directory "${compose_dir}" up -d --no-deps frigate
