"""Render a validated deployment without modifying its source checkout."""

from __future__ import annotations

import hashlib
import json
import re
import secrets as secrets_module
import shutil
import urllib.parse
from pathlib import Path

from .layout import RuntimeLayout, create_staging_layout
from .model import DeploymentConfig, GroupConfig


class RenderError(ValueError):
    """A deployment cannot be represented safely by the current templates."""


FRIGATE_IMAGE = (
    "ghcr.io/blakeblackshear/frigate:0.17.2@"
    "sha256:d4351369984d4a9e2a49ac59736f6490856a7ea11f7790040746d21496967010"
)
CLOUDFLARED_IMAGE = (
    "cloudflare/cloudflared:2026.7.3@"
    "sha256:e39ee8da81ad5e05d77f38d2f51c60ca51bf2a8450ac3abab50c17fdb91d91bf"
)


def _write(path: Path, content: str | bytes, mode: int) -> None:
    data = content.encode("utf-8") if isinstance(content, str) else content
    with path.open("xb") as handle:
        handle.write(data)
    path.chmod(mode)


def _read_camera_password(path: Path, label: str) -> str:
    try:
        password = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise RenderError(f"cannot read {label}: {error}") from error
    if not password or any(character in password for character in "\x00\r\n"):
        raise RenderError(f"{label} must be non-empty UTF-8 without line breaks")
    return password


def _read_token(path: Path, label: str) -> bytes:
    try:
        token = path.read_bytes()
    except OSError as error:
        raise RenderError(f"cannot read {label}: {error}") from error
    if not token:
        raise RenderError(f"{label} cannot be empty")
    return token


def _env_value(value: object) -> str:
    text = str(value)
    if any(character in text for character in "\x00\r\n"):
        raise RenderError("environment values cannot contain NUL or line breaks")
    # Compose interpolates dollar-prefixed expressions in double-quoted dotenv
    # values. A doubled dollar is its documented literal-dollar escape.
    return json.dumps(text, ensure_ascii=False).replace("$", "$$")


def _render_env(values: list[tuple[str, object]]) -> str:
    return "".join(f"{key}={_env_value(value)}\n" for key, value in values)


def _replace_markers(template: str, replacements: dict[str, str]) -> str:
    rendered = template
    for marker, value in replacements.items():
        if rendered.count(marker) != 1:
            raise RenderError(f"template marker must occur exactly once: {marker}")
        rendered = rendered.replace(marker, value)
    if re.search(r"@@[A-Z0-9_]+@@", rendered):
        raise RenderError("unresolved template marker")
    return rendered


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _active_groups(config: DeploymentConfig) -> tuple[GroupConfig, ...]:
    groups = tuple(group for group in config.groups if group.enabled)
    if not 1 <= len(groups) <= 2:
        raise RenderError("rendering requires between one and two enabled groups")
    for group in groups:
        if not 1 <= len(group.streams) <= 2:
            raise RenderError(
                f"enabled group {group.id} must contain between one and two streams"
            )
        if any(character in group.name for character in "=|:;"):
            raise RenderError(f"group {group.id} name contains a Portal delimiter")
        for stream in group.streams:
            if any(character in stream.name for character in "=,"):
                raise RenderError(f"stream {stream.id} name contains a Portal delimiter")
    return groups


def _portal_values(groups: tuple[GroupConfig, ...]) -> tuple[str, str]:
    camera_items: list[str] = []
    group_items: list[str] = []
    for group in groups:
        stream_ids: list[str] = []
        for stream in group.streams:
            camera_items.append(f"{stream.id}={stream.name}")
            stream_ids.append(stream.id)
        stream_ids.extend("" for _ in range(2 - len(stream_ids)))
        group_items.append(f"{group.id}={group.name}:{'|'.join(stream_ids)}")
    return ",".join(camera_items), ";".join(group_items)


def _frigate_sections(
    groups: tuple[GroupConfig, ...],
) -> tuple[str, str, list[tuple[str, str, Path]]]:
    go2rtc_lines: list[str] = []
    camera_lines: list[str] = []
    secret_specs: list[tuple[str, str, Path]] = []
    for group_number, group in enumerate(groups, start=1):
        secret_name = f"camera_password_{group_number}"
        secret_target = f"FRIGATE_RTSP_PASSWORD_GROUP_{group_number}"
        runtime_path = Path(f"camera-password-{group_number}")
        secret_specs.append((secret_name, secret_target, runtime_path))
        encoded_user = urllib.parse.quote(group.rtsp_user, safe="")
        preset = (
            "preset-record-generic-audio-aac"
            if group.record_audio
            else "preset-record-generic"
        )
        for stream in group.streams:
            source = (
                f"rtsp://{encoded_user}:{{{secret_target}}}@{group.host}:"
                f"{group.rtsp_port}{stream.path}"
            )
            go2rtc_lines.extend(
                (
                    f"    {stream.id}:",
                    f"      - {_yaml_string(source)}",
                )
            )
            camera_lines.extend(
                (
                    f"  {stream.id}:",
                    "    ffmpeg:",
                    "      output_args:",
                    f"        record: {preset}",
                    "      inputs:",
                    f"        - path: rtsp://127.0.0.1:8554/{stream.id}",
                    "          input_args: preset-rtsp-restream",
                    "          roles:",
                    "            - record",
                    "            - detect",
                    "    live:",
                    "      streams:",
                    f"        高清直播: {stream.id}",
                )
            )
    return "\n".join(go2rtc_lines), "\n".join(camera_lines), secret_specs


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render_runtime(
    config: DeploymentConfig,
    *,
    source_root: Path,
    staging_parent: Path,
    lan_access_key: str | None = None,
) -> RuntimeLayout:
    """Render into a fresh staging directory without replacing any runtime."""

    source = source_root.expanduser().resolve()
    staging = staging_parent.expanduser().resolve()
    if staging == source or source in staging.parents:
        raise RenderError("staging_parent must be outside the source checkout")
    base_template_path = source / "deploy" / "compose.base.yaml"
    intel_template_path = source / "deploy" / "compose.intel.yaml"
    frigate_template_path = source / "deploy" / "frigate.config.yml.tmpl"
    caddy_source = source / "camera-portal-lan" / "caddy" / "Caddyfile"
    required = (base_template_path, frigate_template_path, caddy_source)
    if config.hardware.mode == "intel-vaapi":
        required += (intel_template_path,)
    missing = tuple(path for path in required if not path.is_file())
    if missing:
        raise RenderError(f"missing render source: {missing[0]}")

    groups = _active_groups(config)
    cameras_value, groups_value = _portal_values(groups)
    go2rtc, cameras, secret_specs = _frigate_sections(groups)
    access_key = (
        secrets_module.token_hex(32)
        if lan_access_key is None
        else lan_access_key
    )
    if len(access_key) < 32 or any(
        character in access_key for character in "\x00\r\n"
    ):
        raise RenderError("lan_access_key must contain at least 32 safe characters")

    local_origin = f"http://{config.lan.bind_ip}:{config.lan.port}"
    admin_disabled = config.provider == "lan"
    public_origin = local_origin if admin_disabled else config.public.origin
    layout = create_staging_layout(
        staging,
        config.instance_id,
        include_intel=config.hardware.mode == "intel-vaapi",
    )
    try:
        service_secret_lines: list[str] = []
        top_secret_lines: list[str] = [
            "  lan_access_key:",
            "    file: ./secrets/lan-access-key",
        ]
        _write(layout.secrets / "lan-access-key", access_key, 0o600)
        for group, (secret_name, secret_target, runtime_name) in zip(
            groups, secret_specs
        ):
            password = _read_camera_password(
                group.password_file, f"group {group.id} password_file"
            )
            encoded_password = urllib.parse.quote(password, safe="")
            _write(layout.secrets / runtime_name, encoded_password, 0o600)
            service_secret_lines.extend(
                (
                    f"      - source: {secret_name}",
                    f"        target: {secret_target}",
                )
            )
            top_secret_lines.extend(
                (
                    f"  {secret_name}:",
                    f"    file: ./secrets/{runtime_name.as_posix()}",
                )
            )

        cloudflared_service = ""
        if config.provider == "cloudflare":
            token_path = config.public.cloudflare_token_file
            if token_path is None:
                raise RenderError("cloudflare provider requires a token file")
            _write(
                layout.secrets / "cloudflare-token",
                _read_token(token_path, "cloudflare_token_file"),
                0o600,
            )
            cloudflared_service = """  cloudflared:
    image: "${CLOUDFLARED_IMAGE:?set CLOUDFLARED_IMAGE in .env}"
    restart: unless-stopped
    init: true
    read_only: true
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
    pids_limit: 64
    mem_limit: 128m
    cpus: 0.35
    command: ["tunnel", "--no-autoupdate", "run"]
    environment:
      TUNNEL_TOKEN_FILE: /run/secrets/TUNNEL_TOKEN
    secrets:
      - source: cloudflare_token
        target: TUNNEL_TOKEN
    depends_on:
      camera-portal:
        condition: service_healthy
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
"""
            top_secret_lines.extend(
                (
                    "  cloudflare_token:",
                    "    file: ./secrets/cloudflare-token",
                )
            )

        compose = _replace_markers(
            base_template_path.read_text(encoding="utf-8"),
            {
                "@@FRIGATE_SERVICE_SECRETS@@": "\n".join(service_secret_lines),
                "@@CLOUDFLARED_SERVICE@@": cloudflared_service.rstrip(),
                "@@TOP_LEVEL_SECRETS@@": "\n".join(top_secret_lines),
            },
        )
        hwaccel = (
            "  hwaccel_args: preset-vaapi"
            if config.hardware.mode == "intel-vaapi"
            else ""
        )
        frigate = _replace_markers(
            frigate_template_path.read_text(encoding="utf-8"),
            {
                "@@HWACCEL@@": hwaccel,
                "@@GO2RTC_STREAMS@@": go2rtc,
                "@@RETENTION_DAYS@@": f"{config.retention:g}",
                "@@CAMERAS@@": cameras,
            },
        )
        environment_values: list[tuple[str, object]] = [
            ("CAMARE_INSTANCE_ID", config.instance_id),
            ("CAMARE_SOURCE_ROOT", source.as_posix()),
            ("FRIGATE_IMAGE", FRIGATE_IMAGE),
            ("CAMERA_PORTAL_IMAGE", f"local/camare-{config.instance_id}-portal:latest"),
            ("LAN_CADDY_IMAGE", f"local/camare-{config.instance_id}-caddy:2.11.4"),
            ("CLOUDFLARED_IMAGE", CLOUDFLARED_IMAGE),
            ("CAMARE_MEDIA_PATH", config.media.as_posix()),
            ("FRIGATE_ADMIN_BIND_IP", config.admin.frigate_bind_ip),
            ("FRIGATE_ADMIN_PORT", config.admin.frigate_port),
            ("PUBLIC_LOOPBACK_PORT", config.public.loopback_port),
            ("ADMIN_LOOPBACK_PORT", config.admin.loopback_port),
            ("PUBLIC_BASE_URL", public_origin),
            ("LOCAL_BASE_URL", local_origin),
            ("ADMIN_DISABLED", str(admin_disabled).lower()),
            ("ADMIN_ORIGIN", "" if admin_disabled else config.admin.origin),
            (
                "ADMIN_TAILSCALE_LOGIN",
                "" if admin_disabled else config.admin.tailscale_login,
            ),
            ("RECORDING_TIMEZONE", config.timezone),
            ("CAMERAS", cameras_value),
            ("CAMERA_GROUPS", groups_value),
            ("LAN_BIND_IP", config.lan.bind_ip),
            ("LAN_PORT", config.lan.port),
        ]
        if config.hardware.mode == "intel-vaapi":
            environment_values.extend(
                (
                    ("FRIGATE_RENDER_DEVICE", config.hardware.render_device),
                    ("LIBVA_DRIVER_NAME", config.hardware.va_driver),
                )
            )

        _write(layout.base_compose, compose.rstrip() + "\n", 0o640)
        if layout.intel_compose is not None:
            _write(
                layout.intel_compose,
                intel_template_path.read_text(encoding="utf-8"),
                0o640,
            )
        _write(layout.frigate_config, frigate.rstrip() + "\n", 0o640)
        # This file is non-secret and is read by the unprivileged Caddy UID.
        _write(layout.caddyfile, caddy_source.read_bytes(), 0o644)
        _write(layout.env, _render_env(environment_values), 0o600)

        non_secret_paths = [
            layout.env,
            layout.base_compose,
            *(() if layout.intel_compose is None else (layout.intel_compose,)),
            layout.frigate_config,
            layout.caddyfile,
        ]
        manifest = {
            "format_version": 1,
            "instance_id": config.instance_id,
            "provider": config.provider,
            "hardware": config.hardware.mode,
            "build_mode": "local",
            "compose_files": [
                path.relative_to(layout.root).as_posix()
                for path in layout.compose_files
            ],
            "files": {
                path.relative_to(layout.root).as_posix(): _digest(path)
                for path in sorted(non_secret_paths)
            },
            "required_ownership": {
                "portal-data": {
                    "uid": 65532,
                    "gid": 65532,
                    "mode": "0750",
                }
            },
        }
        _write(
            layout.manifest,
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            0o640,
        )
        return layout
    except Exception:
        shutil.rmtree(layout.root, ignore_errors=True)
        raise
