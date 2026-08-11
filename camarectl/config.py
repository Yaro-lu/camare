"""Strict INI loader for the public camare deployment configuration."""

from __future__ import annotations

import configparser
import ipaddress
import os
import re
import stat
import urllib.parse
from pathlib import Path, PurePosixPath

from .model import (
    AdminConfig,
    DeploymentConfig,
    GroupConfig,
    HardwareConfig,
    LanConfig,
    PublicConfig,
    StreamConfig,
)


class ConfigError(ValueError):
    """A user-actionable deployment configuration error."""


CAMARE_KEYS = frozenset(
    {
        "instance_id",
        "runtime_dir",
        "timezone",
        "provider",
        "groups",
    }
)
PUBLIC_KEYS = frozenset({"origin", "loopback_port", "cloudflare_token_file"})
ADMIN_KEYS = frozenset(
    {
        "origin",
        "loopback_port",
        "tailscale_login",
        "frigate_bind_ip",
        "frigate_port",
    }
)
LAN_KEYS = frozenset({"bind_ip", "port"})
STORAGE_KEYS = frozenset({"media", "retention"})
HARDWARE_KEYS = frozenset({"mode", "render_device", "va_driver"})
GROUP_KEYS = frozenset(
    {
        "name",
        "host",
        "rtsp_user",
        "rtsp_port",
        "password_file",
        "record_audio",
        "enabled",
        "streams",
    }
)
STREAM_KEYS = frozenset({"name", "path"})
SAFE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SAFE_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
RFC1918_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
SAFE_ADMIN_NETWORKS = RFC1918_NETWORKS + (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
)


def _split_ids(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _validate_id(value: str, label: str) -> str:
    if not SAFE_ID.fullmatch(value):
        raise ConfigError(
            f"unsafe {label} id {value!r}; use 1-32 lowercase letters, digits, _ or -"
        )
    return value


def _validate_port(value: int, label: str) -> int:
    if not 1 <= value <= 65535:
        raise ConfigError(f"{label} must be between 1 and 65535")
    return value


def _validate_nonempty(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized or any(character in normalized for character in "\x00\r\n"):
        raise ConfigError(f"{label} cannot be empty")
    return normalized


def _validate_camera_host(value: str, label: str) -> str:
    host = _validate_nonempty(value, label)
    if any(character in host for character in "/@"):
        raise ConfigError(f"{label} must be an IPv4 address or hostname, not a URL")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not SAFE_HOSTNAME.fullmatch(host):
            raise ConfigError(f"{label} must be an IPv4 address or hostname")
        return host.lower()
    if address.version != 4:
        raise ConfigError(f"{label} must be an IPv4 address or hostname")
    return str(address)


def _validate_rtsp_path(value: str, label: str) -> str:
    path = value.strip()
    if (
        not path.startswith("/")
        or any(character.isspace() for character in path)
        or any(character in path for character in "\x00{}")
    ):
        raise ConfigError(
            f"{label} must start with / and contain no whitespace or template braces"
        )
    return path


def _parse_bool(
    section: configparser.SectionProxy,
    key: str,
    label: str,
) -> bool:
    value = section[key].strip()
    if value not in {"true", "false"}:
        raise ConfigError(f"{label} must be true or false")
    return value == "true"


def _validate_bind_ip(
    value: str,
    label: str,
    allowed_networks: tuple[ipaddress.IPv4Network, ...],
) -> str:
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError as error:
        raise ConfigError(f"{label} must be an IPv4 address") from error
    if address.version != 4 or not any(
        address in network for network in allowed_networks
    ):
        raise ConfigError(f"{label} is outside the allowed private networks")
    return str(address)


def _require_one_or_two(values: tuple[str, ...], label: str) -> None:
    if not 1 <= len(values) <= 2:
        raise ConfigError(f"{label} must contain between 1 and 2 ids")


def _require_unique_ids(values: tuple[str, ...], label: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ConfigError(f"duplicate {label} id: {value}")
        seen.add(value)


def _require_exact_keys(
    section: configparser.SectionProxy, expected: frozenset[str], name: str
) -> None:
    actual = set(section.keys())
    unknown = sorted(actual - expected)
    if unknown:
        raise ConfigError(f"unknown key in [{name}]: {unknown[0]}")
    missing = sorted(expected - actual)
    if missing:
        raise ConfigError(f"missing key in [{name}]: {missing[0]}")


def _resolve_optional_path(value: str, config_path: Path) -> Path | None:
    candidate = value.strip()
    if not candidate:
        return None
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


def _resolve_secret_path(value: str, config_path: Path, label: str) -> Path:
    path = _resolve_optional_path(value, config_path)
    if path is None:
        raise ConfigError(f"{label} is required")
    if not path.exists():
        raise ConfigError(f"{label} does not exist: {path}")
    if not path.is_file():
        raise ConfigError(f"{label} is not a regular file: {path}")
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        if not mode & stat.S_IRUSR or mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ConfigError(
                f"{label} must be owner-readable and inaccessible to group/others; "
                f"run: chmod 600 {path}"
            )
    return path


def _parse_hardware(section: configparser.SectionProxy) -> HardwareConfig:
    mode = section["mode"].strip()
    render_value = section["render_device"].strip()
    driver_value = section["va_driver"].strip()
    if mode == "cpu":
        if render_value or driver_value:
            raise ConfigError(
                "hardware cpu mode requires empty render_device and va_driver"
            )
        return HardwareConfig(mode=mode, render_device=None, va_driver=None)
    if mode != "intel-vaapi":
        raise ConfigError("hardware mode must be cpu or intel-vaapi")
    if not re.fullmatch(r"/dev/dri/renderD[0-9]+", render_value):
        raise ConfigError(
            "hardware intel-vaapi render_device must be /dev/dri/renderD<number>"
        )
    if driver_value not in {"i965", "iHD"}:
        raise ConfigError("hardware intel-vaapi va_driver must be i965 or iHD")
    return HardwareConfig(
        mode=mode,
        render_device=PurePosixPath(render_value),
        va_driver=driver_value,
    )


def _validate_https_origin(value: str, label: str) -> str:
    origin = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(origin)
    try:
        port = parsed.port
    except ValueError as error:
        raise ConfigError(f"{label} has an invalid port") from error
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ConfigError(f"{label} must be an HTTPS origin without a path")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        raise ConfigError(f"{label} must use a hostname, not an IP address")
    return origin


def _validate_access(
    provider: str,
    public_origin: str,
    admin_origin: str,
    tailscale_login: str,
) -> tuple[str, str, str]:
    public_origin = public_origin.strip()
    admin_origin = admin_origin.strip()
    tailscale_login = tailscale_login.strip()
    if provider == "lan":
        if public_origin or admin_origin or tailscale_login:
            raise ConfigError(
                "provider lan requires empty public/admin origins and tailscale_login"
            )
        return "", "", ""
    public_origin = _validate_https_origin(public_origin, "public origin")
    admin_origin = _validate_https_origin(admin_origin, "admin origin")
    admin_host = (urllib.parse.urlsplit(admin_origin).hostname or "").lower()
    if not admin_host.endswith(".ts.net"):
        raise ConfigError("admin origin must use a Tailscale .ts.net hostname")
    if not tailscale_login:
        raise ConfigError("tailscale_login is required for public providers")
    if provider == "tailscale":
        public_host = (urllib.parse.urlsplit(public_origin).hostname or "").lower()
        if not public_host.endswith(".ts.net"):
            raise ConfigError("tailscale public origin must use a .ts.net hostname")
    return public_origin, admin_origin, tailscale_login


def _validate_posix_runtime_path(value: str, label: str) -> PurePosixPath:
    path = PurePosixPath(value.strip())
    forbidden_roots = {"boot", "dev", "etc", "proc", "root", "run", "sys", "usr"}
    if (
        not path.is_absolute()
        or ".." in path.parts
        or len(path.parts) < 3
        or path.parts[1] in forbidden_roots
    ):
        raise ConfigError(
            f"{label} must be a dedicated absolute Linux path without .."
        )
    return path


def _validate_timezone(value: str) -> str:
    timezone = value.strip()
    if (
        len(timezone) > 255
        or not re.fullmatch(
            r"(?:UTC|[A-Za-z][A-Za-z0-9_+.-]*(?:/[A-Za-z0-9][A-Za-z0-9_+.-]*)+)",
            timezone,
        )
    ):
        raise ConfigError(f"invalid timezone: {timezone}")
    return timezone


def _validate_retention(value: float) -> float:
    if not 0 < value <= 365:
        raise ConfigError("retention must be greater than 0 and at most 365 days")
    return value


def load_config(path: Path) -> DeploymentConfig:
    """Load a deployment configuration without reading referenced secrets."""

    config_path = path.expanduser().resolve()
    parser = configparser.ConfigParser(
        interpolation=None,
        strict=True,
        empty_lines_in_values=False,
    )
    parser.optionxform = str
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as error:
        raise ConfigError(f"cannot read configuration {config_path}: {error}") from error

    try:
        root = parser["camare"]
        _require_exact_keys(root, CAMARE_KEYS, "camare")
        public_section = parser["public"]
        admin_section = parser["admin"]
        lan_section = parser["lan"]
        storage_section = parser["storage"]
        hardware_section = parser["hardware"]
        _require_exact_keys(public_section, PUBLIC_KEYS, "public")
        _require_exact_keys(admin_section, ADMIN_KEYS, "admin")
        _require_exact_keys(lan_section, LAN_KEYS, "lan")
        _require_exact_keys(storage_section, STORAGE_KEYS, "storage")
        _require_exact_keys(hardware_section, HARDWARE_KEYS, "hardware")
        allowed_sections = {
            "camare",
            "public",
            "admin",
            "lan",
            "storage",
            "hardware",
        }
        instance_id = _validate_id(root["instance_id"].strip(), "instance")
        provider = root["provider"].strip()
        if provider not in {"lan", "cloudflare", "tailscale"}:
            raise ConfigError("provider must be lan, cloudflare or tailscale")
        token_value = public_section["cloudflare_token_file"].strip()
        if provider == "cloudflare":
            cloudflare_token_file = _resolve_secret_path(
                token_value,
                config_path,
                "cloudflare_token_file",
            )
        else:
            if token_value:
                raise ConfigError(
                    "cloudflare_token_file must be empty unless provider is cloudflare"
                )
            cloudflare_token_file = None
        public_origin, admin_origin, tailscale_login = _validate_access(
            provider,
            public_section["origin"],
            admin_section["origin"],
            admin_section["tailscale_login"],
        )
        runtime_dir = _validate_posix_runtime_path(
            root["runtime_dir"], "runtime_dir"
        )
        media = _validate_posix_runtime_path(storage_section["media"], "media")
        timezone = _validate_timezone(root["timezone"])
        retention = _validate_retention(storage_section.getfloat("retention"))
        public_loopback_port = _validate_port(
            public_section.getint("loopback_port"), "public loopback_port"
        )
        admin_loopback_port = _validate_port(
            admin_section.getint("loopback_port"), "admin loopback_port"
        )
        frigate_bind_ip = _validate_bind_ip(
            admin_section["frigate_bind_ip"],
            "frigate_bind_ip",
            SAFE_ADMIN_NETWORKS,
        )
        frigate_port = _validate_port(
            admin_section.getint("frigate_port"), "frigate_port"
        )
        lan_bind_ip = _validate_bind_ip(
            lan_section["bind_ip"], "lan bind_ip", RFC1918_NETWORKS
        )
        lan_port = _validate_port(lan_section.getint("port"), "lan port")
        group_ids = _split_ids(root["groups"])
        _require_one_or_two(group_ids, "groups")
        _require_unique_ids(group_ids, "group")
        groups: list[GroupConfig] = []
        global_stream_ids: set[str] = set()
        for group_id in group_ids:
            _validate_id(group_id, "group")
            group_section_name = f"group:{group_id}"
            allowed_sections.add(group_section_name)
            group_section = parser[group_section_name]
            _require_exact_keys(group_section, GROUP_KEYS, group_section_name)
            stream_ids = _split_ids(group_section["streams"])
            _require_one_or_two(stream_ids, f"group {group_id} streams")
            _require_unique_ids(stream_ids, "stream")
            streams: list[StreamConfig] = []
            for stream_id in stream_ids:
                _validate_id(stream_id, "stream")
                if stream_id in global_stream_ids:
                    raise ConfigError(f"duplicate stream id: {stream_id}")
                global_stream_ids.add(stream_id)
                stream_section_name = f"stream:{group_id}:{stream_id}"
                allowed_sections.add(stream_section_name)
                stream_section = parser[stream_section_name]
                _require_exact_keys(
                    stream_section,
                    STREAM_KEYS,
                    stream_section_name,
                )
                streams.append(
                    StreamConfig(
                        id=stream_id,
                        name=_validate_nonempty(
                            stream_section["name"],
                            f"stream {stream_id} name",
                        ),
                        path=_validate_rtsp_path(
                            stream_section["path"],
                            f"stream {stream_id} path",
                        ),
                    )
                )
            password_file = _resolve_secret_path(
                group_section["password_file"],
                config_path,
                f"group {group_id} password_file",
            )
            groups.append(
                GroupConfig(
                    id=group_id,
                    name=_validate_nonempty(
                        group_section["name"],
                        f"group {group_id} name",
                    ),
                    host=_validate_camera_host(
                        group_section["host"],
                        f"group {group_id} host",
                    ),
                    rtsp_user=_validate_nonempty(
                        group_section["rtsp_user"],
                        f"group {group_id} rtsp_user",
                    ),
                    rtsp_port=_validate_port(
                        group_section.getint("rtsp_port"),
                        f"group {group_id} rtsp_port",
                    ),
                    password_file=password_file,
                    record_audio=_parse_bool(
                        group_section,
                        "record_audio",
                        f"group {group_id} record_audio",
                    ),
                    enabled=_parse_bool(
                        group_section,
                        "enabled",
                        f"group {group_id} enabled",
                    ),
                    streams=tuple(streams),
                )
            )
        unknown_sections = sorted(set(parser.sections()) - allowed_sections)
        if unknown_sections:
            raise ConfigError(f"unknown section: [{unknown_sections[0]}]")
        return DeploymentConfig(
            instance_id=instance_id,
            runtime_dir=runtime_dir,
            timezone=timezone,
            provider=provider,
            public=PublicConfig(
                origin=public_origin,
                loopback_port=public_loopback_port,
                cloudflare_token_file=cloudflare_token_file,
            ),
            admin=AdminConfig(
                origin=admin_origin,
                loopback_port=admin_loopback_port,
                tailscale_login=tailscale_login,
                frigate_bind_ip=frigate_bind_ip,
                frigate_port=frigate_port,
            ),
            lan=LanConfig(
                bind_ip=lan_bind_ip,
                port=lan_port,
            ),
            media=media,
            retention=retention,
            hardware=_parse_hardware(hardware_section),
            groups=tuple(groups),
        )
    except (KeyError, ValueError, configparser.Error) as error:
        raise ConfigError(f"invalid configuration {config_path}: {error}") from error
