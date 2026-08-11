#!/bin/sh
set -eu

root_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$root_dir"

fail() {
	echo "ERROR: $*" >&2
	exit 1
}

[ "$#" -eq 0 ] || fail "usage: $0"
[ -f .env ] || fail ".env is missing"

read_env() {
	key="$1"
	sed -n "s/^${key}=//p" .env | head -n 1
}

lan_ip="$(read_env LAN_BIND_IP)"
lan_port="$(read_env LAN_PORT)"
lan_port="${lan_port:-8088}"
portal_network="$(read_env PORTAL_INGRESS_NETWORK)"
portal_network="${portal_network:-camera_portal_tunnel}"

[ -n "$lan_ip" ] || fail "LAN_BIND_IP is missing"

lan_key="$(
	read_env LAN_ACCESS_KEY
)"
[ "${#lan_key}" -ge 32 ] ||
	fail "LAN_ACCESS_KEY must contain at least 32 characters"
unset lan_key

ip -4 addr show |
	grep -Fq "${lan_ip}/" ||
	fail "host does not own ${lan_ip}"

docker network inspect "$portal_network" >/dev/null 2>&1 ||
	fail "external Docker network ${portal_network} does not exist"

portal_id=""
for container_id in $(
	docker network inspect "$portal_network" \
		--format '{{range $id, $_ := .Containers}}{{$id}} {{end}}'
); do
	service="$(
		docker inspect "$container_id" \
			--format '{{index .Config.Labels "com.docker.compose.service"}}'
	)"
	if [ "$service" = "camera-portal" ]; then
		[ -z "$portal_id" ] ||
			fail "multiple camera-portal services are attached to ${portal_network}"
		portal_id="$container_id"
	fi
done
[ -n "$portal_id" ] ||
	fail "camera-portal Compose service is not attached to ${portal_network}"

# --quiet prevents Compose from printing the rendered LAN secret.
docker compose config --quiet
docker compose build lan-caddy
docker compose run --rm --no-deps --entrypoint caddy lan-caddy \
	validate --config /etc/caddy/Caddyfile --adapter caddyfile

lan_caddy_id="$(docker compose ps -q lan-caddy)"
if [ -n "$lan_caddy_id" ]; then
	running="$(
		docker inspect \
			--format '{{.State.Running}}' \
			"$lan_caddy_id"
	)"
	[ "$running" = "true" ] ||
		fail "lan-caddy exists but is not running"

	health="$(
		docker inspect \
			--format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' \
			"$lan_caddy_id"
	)"
	[ "$health" = "healthy" ] ||
		fail "lan-caddy is not healthy (status: $health)"

	bindings="$(
		docker inspect \
			--format '{{range .NetworkSettings.Ports}}{{range .}}{{printf "%s:%s\n" .HostIp .HostPort}}{{end}}{{end}}' \
			"$lan_caddy_id"
	)"
	[ "$bindings" = "${lan_ip}:${lan_port}" ] ||
		fail "unexpected published binding: ${bindings:-none}"

	root_page="$(
		curl --silent --show-error --fail --max-time 10 \
			"http://${lan_ip}:${lan_port}/"
	)"
	printf '%s\n' "$root_page" |
		grep -Fq '<title>实时画面</title>' ||
		fail "LAN root did not return the viewer directly"

	curl --silent --show-error --fail --max-time 10 \
		"http://${lan_ip}:${lan_port}/api/config" |
		grep -Fq '"cameras"' ||
		fail "LAN API did not allow authentication-free access"

	echo "Runtime checks passed: the auth-free LAN viewer is available only at http://${lan_ip}:${lan_port}."
else
	echo "Static checks passed. Start lan-caddy, then rerun for runtime checks."
fi
