#!/usr/bin/env python3
"""Interactive installer for the camare Docker stack.

The installer intentionally uses only Python's standard library. It writes
runtime configuration and secrets, validates the current host, then starts the
existing Compose projects without removing containers, databases, or media.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import re
import secrets as secrets_module
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parent
PORTAL_DIR = PROJECT_ROOT / "camera-portal"
LAN_DIR = PROJECT_ROOT / "camera-portal-lan"
FRIGATE_DIR = PROJECT_ROOT / "frigate-1panel"
CLOUDFLARE_DIR = PROJECT_ROOT / "examples" / "cloudflare"

PORTAL_ENV = PORTAL_DIR / ".env"
LAN_ENV = LAN_DIR / ".env"
FRIGATE_ENV = FRIGATE_DIR / ".env"
CLOUDFLARE_ENV = CLOUDFLARE_DIR / ".env"
CAMERA_PASSWORD_FILE = FRIGATE_DIR / "secrets" / "FRIGATE_RTSP_PASSWORD"
CLOUDFLARE_TOKEN_FILE = CLOUDFLARE_DIR / "secrets" / "TUNNEL_TOKEN"
FRIGATE_CONFIG = FRIGATE_DIR / "config" / "config.yml"

PROVIDERS = ("cloudflare", "tailscale", "lan")
SAFE_ENV_VALUE = re.compile(r"^[A-Za-z0-9_./:@,+?&=%-]+$")
HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
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


class InstallerError(RuntimeError):
    """A user-actionable installer failure."""


@dataclass(frozen=True)
class InstallConfig:
    lan_ip: str
    lan_port: int
    camera_host: str
    rtsp_user: str
    rtsp_port: int
    ptz_path: str
    fixed_path: str
    ptz_name: str
    fixed_name: str
    media_path: PurePosixPath
    create_media_path: bool
    retention_days: float
    render_device: PurePosixPath
    va_driver: str
    frigate_admin_ip: str
    frigate_admin_port: int
    provider: str
    public_base_url: str
    admin_origin: str
    admin_login: str
    configure_tailscale: bool
    public_loopback_port: int = 9080
    admin_loopback_port: int = 9081
    frigate_network: str = "frigate_default"
    portal_network: str = "camera_portal_tunnel"


@dataclass(frozen=True, repr=False)
class SecretInputs:
    camera_password: str | None
    cloudflare_token: str | None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="交互式生成配置并部署 Frigate + Camera Portal。"
    )
    parser.add_argument(
        "--configure-only",
        action="store_true",
        help="只生成/更新配置，不检查或启动 Docker/Tailscale。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="完成问答和校验，但不写文件、不启动服务。",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="跳过最后一次部署确认；问答仍会进行。",
    )
    return parser.parse_args(argv)


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        values[key] = value
    return values


def encode_env_value(value: str) -> str:
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise InstallerError("配置值不能包含换行或 NUL 字符。")
    if value and SAFE_ENV_VALUE.fullmatch(value) and not value.startswith("#"):
        return value
    return json.dumps(value, ensure_ascii=False)


def render_env(mapping: Mapping[str, str], comments: Iterable[str] = ()) -> str:
    lines = [f"# {comment}" for comment in comments]
    if lines:
        lines.append("")
    lines.extend(f"{key}={encode_env_value(value)}" for key, value in mapping.items())
    return "\n".join(lines) + "\n"


def backup_file(path: Path, mode: int) -> Path | None:
    if not path.exists():
        return None
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{timestamp}")
    counter = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak-{timestamp}-{counter}")
        counter += 1
    shutil.copy2(path, backup)
    os.chmod(backup, mode)
    backups = sorted(
        path.parent.glob(f"{path.name}.bak-*"),
        key=lambda candidate: candidate.stat().st_mtime,
        reverse=True,
    )
    for stale in backups[3:]:
        stale.unlink()
    return backup


def atomic_write(path: Path, content: str, mode: int) -> bool:
    encoded = content.encode("utf-8")
    if path.is_file() and path.read_bytes() == encoded:
        os.chmod(path, mode)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_file(path, mode)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return True


def atomic_write_secret(path: Path, value: str) -> None:
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise InstallerError(f"{path.name} 不能包含换行或 NUL 字符。")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(value.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def validate_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise ValueError("必须是数字") from error
    if not 1 <= port <= 65535:
        raise ValueError("必须位于 1–65535")
    return port


def validate_lan_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ValueError("不是有效 IPv4") from error
    if address.version != 4 or not any(
        address in network for network in RFC1918_NETWORKS
    ):
        raise ValueError("必须是 10/8、172.16/12 或 192.168/16 家庭网 IPv4")
    return str(address)


def validate_bind_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ValueError("不是有效 IPv4") from error
    if address.version != 4 or not any(
        address in network for network in SAFE_ADMIN_NETWORKS
    ):
        raise ValueError("必须是回环、家庭网或 Tailscale IPv4")
    return str(address)


def validate_camera_host(value: str) -> str:
    value = value.strip()
    if not value or any(character in value for character in "\r\n/@"):
        raise ValueError("摄像头地址格式不正确")
    try:
        address = ipaddress.ip_address(value)
        if address.version != 4:
            raise ValueError("首版只支持 IPv4 摄像头地址")
        return str(address)
    except ValueError:
        if not HOSTNAME.fullmatch(value):
            raise ValueError("必须是 IPv4 或合法主机名")
        return value.lower()


def validate_rtsp_path(value: str) -> str:
    value = value.strip()
    if not value.startswith("/") or any(character.isspace() for character in value):
        raise ValueError("必须以 / 开头且不能包含空白字符")
    if any(character in value for character in ("\x00", "\r", "\n", "{", "}")):
        raise ValueError("包含不允许的字符")
    return value


def validate_camera_name(value: str) -> str:
    value = value.strip()
    if not value or any(character in value for character in (",", "=", "\r", "\n")):
        raise ValueError("不能为空，且不能包含逗号、等号或换行")
    return value


def validate_https_origin(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("HTTPS 端口无效") from error
    hostname = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not hostname
        or any(character.isspace() for character in parsed.netloc)
        or "\\" in parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("必须是无路径、查询和账号信息的 HTTPS origin")
    return f"https://{parsed.netloc}"


def validate_deploy_origin(value: str) -> str:
    origin = validate_https_origin(value)
    hostname = (urllib.parse.urlsplit(origin).hostname or "").rstrip(".").lower()
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("公网 origin 必须使用域名，不能直接使用 IP")
    reserved_hosts = {"example.com", "example.net", "example.org", "localhost"}
    if hostname in reserved_hosts or hostname.endswith(
        (
            ".example",
            ".example.com",
            ".example.net",
            ".example.org",
            ".invalid",
            ".localhost",
            ".test",
        )
    ):
        raise ValueError("不能使用示例或保留域名，请填写真实可访问域名")
    return origin


def validate_tailscale_origin(value: str) -> str:
    origin = validate_deploy_origin(value)
    hostname = (urllib.parse.urlsplit(origin).hostname or "").rstrip(".").lower()
    if not hostname.endswith(".ts.net"):
        raise ValueError("Tailscale Serve/Funnel 地址必须使用 .ts.net 域名")
    return origin


def validate_retention(value: str) -> float:
    try:
        days = float(value)
    except ValueError as error:
        raise ValueError("必须是数字") from error
    if not 0 < days <= 365:
        raise ValueError("必须大于 0 且不超过 365 天")
    return days


def validate_nonempty(value: str) -> str:
    value = value.strip()
    if not value or any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError("不能为空或包含换行")
    return value


def validate_linux_absolute_path(value: str) -> str:
    value = validate_nonempty(value)
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("必须是 Linux 绝对路径，且不能包含 ..")
    return str(path)


def validate_media_path(value: str) -> str:
    value = validate_linux_absolute_path(value)
    path = PurePosixPath(value)
    if len(path.parts) < 3 or path.parts[1] in {
        "boot",
        "dev",
        "etc",
        "proc",
        "root",
        "run",
        "sys",
        "usr",
    }:
        raise ValueError("请使用专用子目录，例如 /srv/frigate/media 或 /mnt/nas/camera")
    return value


def validate_render_device(value: str) -> str:
    value = validate_linux_absolute_path(value)
    if not re.fullmatch(r"/dev/dri/renderD[0-9]+", value):
        raise ValueError("首版只接受 /dev/dri/renderD数字 形式的 Intel VAAPI 设备")
    return value


def prompt_value(
    label: str,
    default: str,
    validator: Callable[[str], object] = validate_nonempty,
) -> object:
    while True:
        suffix = f" [{default}]" if default else ""
        raw = input(f"{label}{suffix}: ").strip()
        candidate = raw or default
        try:
            return validator(candidate)
        except ValueError as error:
            print(f"  输入无效：{error}")


def prompt_yes_no(label: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{label} [{hint}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes", "是"):
            return True
        if raw in ("n", "no", "否"):
            return False
        print("  请输入 y 或 n。")


def prompt_provider(default: str) -> str:
    choices = {
        "1": "cloudflare",
        "2": "tailscale",
        "3": "lan",
        "cloudflare": "cloudflare",
        "tailscale": "tailscale",
        "lan": "lan",
    }
    print("\n公网方式：")
    print("  1. Cloudflare Tunnel（客户公网）+ Tailscale Serve（管理）")
    print("  2. Tailscale Funnel（客户公网）+ Serve（管理）")
    print("  3. 仅局域网（暂不启用二维码和管理页）")
    while True:
        raw = input(f"请选择 [默认 {default}]: ").strip().lower()
        candidate = raw or default
        if candidate in choices:
            return choices[candidate]
        print("  请输入 1、2、3 或对应英文名称。")


def prompt_secret(label: str, existing_path: Path) -> str | None:
    exists = existing_path.is_file() and existing_path.stat().st_size > 0
    suffix = "（留空保留现有值）" if exists else ""
    while True:
        value = getpass.getpass(f"{label}{suffix}: ")
        if not value and exists:
            return None
        if not value:
            print("  该值不能为空。")
            continue
        if any(character in value for character in ("\x00", "\r", "\n")):
            print("  不能包含换行或 NUL 字符。")
            continue
        return value


def detect_default_lan_ip() -> str:
    try:
        result = subprocess.run(
            ["ip", "-j", "-4", "route", "get", "1.1.1.1"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        routes = json.loads(result.stdout)
        candidate = routes[0].get("prefsrc", "") if routes else ""
        if candidate:
            return validate_lan_ip(candidate)
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        pass
    return "192.168.1.250"


def detect_tailscale() -> tuple[str, str]:
    if not shutil.which("tailscale"):
        return "", ""
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        status = json.loads(result.stdout)
        own = status.get("Self", {})
        dns_name = str(own.get("DNSName", "")).rstrip(".")
        ipv4 = next(
            (
                address
                for address in own.get("TailscaleIPs", [])
                if ":" not in address
            ),
            "",
        )
        return dns_name, ipv4
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return "", ""


def valid_lan_key(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{64,}", value))


def choose_lan_key(portal_values: Mapping[str, str], lan_values: Mapping[str, str]) -> str:
    portal_key = portal_values.get("LAN_ACCESS_KEY", "")
    lan_key = lan_values.get("LAN_ACCESS_KEY", "")
    if valid_lan_key(portal_key) and valid_lan_key(lan_key) and portal_key != lan_key:
        raise InstallerError(
            "两个现有 .env 的 LAN_ACCESS_KEY 不一致。为避免中断现有局域网，"
            "请先备份并手工决定保留哪一个。"
        )
    if valid_lan_key(portal_key):
        return portal_key
    if valid_lan_key(lan_key):
        return lan_key
    return secrets_module.token_hex(32)


def read_retention_days(path: Path, fallback: float = 0.125) -> float:
    if not path.is_file():
        return fallback
    in_record = False
    in_continuous = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            in_record = stripped == "record:"
            in_continuous = False
        elif in_record and indent == 2:
            in_continuous = stripped == "continuous:"
        elif in_record and in_continuous and indent == 4 and stripped.startswith("days:"):
            try:
                return float(stripped.split(":", 1)[1].strip())
            except ValueError:
                return fallback
    return fallback


def update_retention_days(content: str, days: float) -> str:
    output: list[str] = []
    in_record = False
    in_continuous = False
    replaced = False
    formatted = f"{days:g}"
    for line in content.splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            in_record = stripped == "record:"
            in_continuous = False
        elif in_record and indent == 2:
            in_continuous = stripped == "continuous:"
        elif (
            in_record
            and in_continuous
            and indent == 4
            and stripped.startswith("days:")
        ):
            line = f"    days: {formatted}"
            replaced = True
        output.append(line)
    if not replaced:
        raise InstallerError("Frigate 配置中找不到 record.continuous.days。")
    return "\n".join(output) + "\n"


def collect_answers(
    *, prompt_secrets: bool = True, check_host_paths: bool = True
) -> tuple[InstallConfig, SecretInputs, str]:
    portal_values = parse_env_file(PORTAL_ENV)
    lan_values = parse_env_file(LAN_ENV)
    frigate_values = parse_env_file(FRIGATE_ENV)
    if not FRIGATE_CONFIG.is_file():
        raise InstallerError("项目不完整：缺少 frigate-1panel/config/config.yml。")
    lan_key = choose_lan_key(portal_values, lan_values)
    tailscale_dns, tailscale_ip = detect_tailscale()

    print("\n=== 360 摄像机 NAS 播放回放方案：自动部署 ===")
    print("输入一次参数，脚本会生成配置；密码和 token 不会显示在摘要中。\n")

    lan_ip = prompt_value(
        "服务器家庭网 IPv4",
        lan_values.get("LAN_BIND_IP", detect_default_lan_ip()),
        validate_lan_ip,
    )
    lan_port = prompt_value(
        "局域网观看端口",
        lan_values.get("LAN_PORT", "8088"),
        validate_port,
    )
    camera_host = prompt_value(
        "摄像头 IPv4/主机名",
        frigate_values.get("FRIGATE_CAMERA_HOST", "192.168.1.128"),
        validate_camera_host,
    )
    rtsp_user_raw = prompt_value(
        "RTSP 用户名",
        urllib.parse.unquote(frigate_values.get("FRIGATE_RTSP_USER", "admin")),
        validate_nonempty,
    )
    rtsp_port = prompt_value(
        "RTSP 端口",
        frigate_values.get("FRIGATE_RTSP_PORT", "554"),
        validate_port,
    )
    ptz_path = prompt_value(
        "第一路 RTSP 路径",
        frigate_values.get("FRIGATE_PTZ_PATH", "/live/101"),
        validate_rtsp_path,
    )
    fixed_path = prompt_value(
        "第二路 RTSP 路径",
        frigate_values.get("FRIGATE_FIXED_PATH", "/live/201"),
        validate_rtsp_path,
    )

    existing_cameras = portal_values.get(
        "CAMERAS", "ptz_view=云台画面,fixed_view=枪机画面"
    )
    camera_names = {}
    for item in existing_cameras.split(","):
        if "=" in item:
            identifier, name = item.split("=", 1)
            camera_names[identifier] = name
    ptz_name = prompt_value(
        "第一路显示名称", camera_names.get("ptz_view", "云台画面"), validate_camera_name
    )
    fixed_name = prompt_value(
        "第二路显示名称",
        camera_names.get("fixed_view", "枪机画面"),
        validate_camera_name,
    )

    media_default = frigate_values.get("FRIGATE_MEDIA_PATH", "/srv/frigate/media")
    media_path = PurePosixPath(
        str(prompt_value("录像目录", media_default, validate_media_path))
    )
    create_media = False
    if check_host_paths and not Path(media_path).exists():
        create_media = prompt_yes_no(
            f"{media_path} 不存在，部署时创建这个普通目录吗", default=False
        )
        if not create_media:
            raise InstallerError(
                "录像目录不存在。若它属于外接盘/NAS，请先完成挂载后重新运行。"
            )

    retention_days = prompt_value(
        "连续录像保留天数",
        f"{read_retention_days(FRIGATE_CONFIG):g}",
        validate_retention,
    )

    render_device = PurePosixPath(
        str(
            prompt_value(
                "Intel VAAPI render device",
                frigate_values.get(
                    "FRIGATE_RENDER_DEVICE", "/dev/dri/renderD128"
                ),
                validate_render_device,
            )
        )
    )
    va_driver = str(
        prompt_value(
            "VAAPI 驱动（老 Intel 通常 i965，新 Intel 通常 iHD）",
            frigate_values.get("LIBVA_DRIVER_NAME", "i965"),
            validate_nonempty,
        )
    )

    provider_default = "cloudflare"
    public_existing = portal_values.get("PUBLIC_BASE_URL", "")
    if ".ts.net" in public_existing:
        provider_default = "tailscale"
    elif public_existing.endswith(".invalid"):
        provider_default = "lan"
    provider = prompt_provider(provider_default)

    public_base_url = "https://camera.invalid"
    admin_origin = "https://admin.invalid"
    admin_login = "disabled@example.invalid"
    configure_tailscale = False
    if provider == "cloudflare":
        cloudflare_default = (
            public_existing
            if public_existing
            and ".ts.net" not in public_existing
            and not public_existing.endswith(".invalid")
            else ""
        )
        public_base_url = prompt_value(
            "Cloudflare 公网 HTTPS origin",
            cloudflare_default,
            validate_deploy_origin,
        )
    elif provider == "tailscale":
        default_public = (
            f"https://{tailscale_dns}:8443"
            if tailscale_dns
            else ""
        )
        public_base_url = prompt_value(
            "Tailscale Funnel HTTPS origin",
            public_existing
            if ".ts.net" in public_existing
            else default_public,
            validate_tailscale_origin,
        )

    if provider in ("cloudflare", "tailscale"):
        default_admin = (
            f"https://{tailscale_dns}:9443"
            if tailscale_dns
            else ""
        )
        admin_existing = portal_values.get("ADMIN_ORIGIN", "")
        admin_origin = prompt_value(
            "Tailscale Serve 管理 HTTPS origin",
            admin_existing
            if ".ts.net" in admin_existing
            and not admin_existing.endswith(".invalid")
            else default_admin,
            validate_tailscale_origin,
        )
        admin_login = prompt_value(
            "允许进入管理页的 Tailscale 登录名",
            portal_values.get("ADMIN_TAILSCALE_LOGIN", "you@example.com"),
            validate_nonempty,
        )
        configure_tailscale = prompt_yes_no(
            "部署后自动执行 Tailscale Serve"
            + (" 和 Funnel" if provider == "tailscale" else ""),
            default=bool(shutil.which("tailscale")),
        )
        if configure_tailscale:
            admin_port = urllib.parse.urlsplit(str(admin_origin)).port
            if admin_port != 9443:
                raise InstallerError(
                    "自动配置 Tailscale Serve 时，管理 origin 必须使用 :9443。"
                )
            if (
                provider == "tailscale"
                and urllib.parse.urlsplit(str(public_base_url)).port != 8443
            ):
                raise InstallerError(
                    "自动配置 Tailscale Funnel 时，客户 origin 必须使用 :8443。"
                )

    frigate_admin_ip = prompt_value(
        "Frigate 管理页绑定 IPv4",
        frigate_values.get("FRIGATE_ADMIN_BIND_IP", tailscale_ip or str(lan_ip)),
        validate_bind_ip,
    )
    frigate_admin_port = prompt_value(
        "Frigate 管理端口",
        frigate_values.get("FRIGATE_ADMIN_PORT", "8971"),
        validate_port,
    )

    camera_password: str | None = None
    cloudflare_token: str | None = None
    if prompt_secrets:
        camera_password = prompt_secret("摄像头 RTSP 密码", CAMERA_PASSWORD_FILE)
        if provider == "cloudflare":
            cloudflare_token = prompt_secret(
                "Cloudflare Tunnel token", CLOUDFLARE_TOKEN_FILE
            )

    config = InstallConfig(
        lan_ip=str(lan_ip),
        lan_port=int(lan_port),
        camera_host=str(camera_host),
        rtsp_user=urllib.parse.quote(str(rtsp_user_raw), safe=""),
        rtsp_port=int(rtsp_port),
        ptz_path=str(ptz_path),
        fixed_path=str(fixed_path),
        ptz_name=str(ptz_name),
        fixed_name=str(fixed_name),
        media_path=media_path,
        create_media_path=create_media,
        retention_days=float(retention_days),
        render_device=render_device,
        va_driver=va_driver,
        frigate_admin_ip=str(frigate_admin_ip),
        frigate_admin_port=int(frigate_admin_port),
        provider=provider,
        public_base_url=str(public_base_url),
        admin_origin=str(admin_origin),
        admin_login=str(admin_login),
        configure_tailscale=configure_tailscale,
    )
    return config, SecretInputs(camera_password, cloudflare_token), lan_key


def config_summary(config: InstallConfig) -> str:
    provider_names = {
        "cloudflare": "Cloudflare Tunnel",
        "tailscale": "Tailscale Funnel",
        "lan": "仅局域网",
    }
    lines = [
        "",
        "=== 配置摘要（不显示秘密） ===",
        f"局域网入口: http://{config.lan_ip}:{config.lan_port}/",
        f"摄像头: {config.camera_host}:{config.rtsp_port}"
        f"{config.ptz_path} + {config.fixed_path}",
        f"录像目录: {config.media_path}",
        f"保留天数: {config.retention_days:g}",
        f"硬件解码: {config.render_device} / {config.va_driver}",
        f"Frigate 管理: https://{config.frigate_admin_ip}:"
        f"{config.frigate_admin_port}/",
        f"公网方式: {provider_names[config.provider]}",
    ]
    if config.provider != "lan":
        lines.extend(
            [
                f"客户入口: {config.public_base_url}/",
                f"管理入口: {config.admin_origin}/",
            ]
        )
    else:
        lines.append("二维码/公网管理: 暂未启用")
    return "\n".join(lines)


def build_env_mappings(
    config: InstallConfig, lan_key: str
) -> dict[Path, dict[str, str]]:
    frigate_mapping = parse_env_file(FRIGATE_ENV)
    portal_mapping = parse_env_file(PORTAL_ENV)
    lan_mapping = parse_env_file(LAN_ENV)
    cloudflare_mapping = parse_env_file(CLOUDFLARE_ENV)

    frigate_mapping.pop("FRIGATE_RTSP_PASSWORD", None)
    cloudflare_mapping.pop("TUNNEL_TOKEN", None)

    frigate_mapping.update(
        {
            "FRIGATE_CAMERA_HOST": config.camera_host,
            "FRIGATE_RTSP_USER": config.rtsp_user,
            "FRIGATE_RTSP_PORT": str(config.rtsp_port),
            "FRIGATE_PTZ_PATH": config.ptz_path,
            "FRIGATE_FIXED_PATH": config.fixed_path,
            "FRIGATE_MEDIA_PATH": str(config.media_path),
            "FRIGATE_ADMIN_BIND_IP": config.frigate_admin_ip,
            "FRIGATE_ADMIN_PORT": str(config.frigate_admin_port),
            "FRIGATE_RENDER_DEVICE": str(config.render_device),
            "LIBVA_DRIVER_NAME": config.va_driver,
            "FRIGATE_DOCKER_NETWORK": config.frigate_network,
            "CAMERA_MAC": frigate_mapping.get(
                "CAMERA_MAC", "00:11:22:33:44:55"
            ),
            "CAMERA_SUBNET": frigate_mapping.get(
                "CAMERA_SUBNET", ".".join(config.lan_ip.split(".")[:3])
            ),
        }
    )
    portal_mapping.update(
        {
            "PUBLIC_BASE_URL": config.public_base_url,
            "LOCAL_BASE_URL": f"http://{config.lan_ip}:{config.lan_port}",
            "LAN_ACCESS_KEY": lan_key,
            "ADMIN_ORIGIN": config.admin_origin,
            "ADMIN_TAILSCALE_LOGIN": config.admin_login,
            "RECORDING_TIMEZONE": "Asia/Shanghai",
            "CAMERAS": (
                f"ptz_view={config.ptz_name},fixed_view={config.fixed_name}"
            ),
            "CAMERA_GROUPS": portal_mapping.get(
                "CAMERA_GROUPS",
                "yard=院子:ptz_view|fixed_view;front=门前:|",
            ),
            "PUBLIC_LOOPBACK_PORT": str(config.public_loopback_port),
            "ADMIN_LOOPBACK_PORT": str(config.admin_loopback_port),
            "FRIGATE_DOCKER_NETWORK": config.frigate_network,
            "PORTAL_INGRESS_NETWORK": config.portal_network,
        }
    )
    lan_mapping.update(
        {
            "LAN_BIND_IP": config.lan_ip,
            "LAN_PORT": str(config.lan_port),
            "LAN_ACCESS_KEY": lan_key,
            "PORTAL_INGRESS_NETWORK": config.portal_network,
        }
    )
    cloudflare_mapping.update(
        {
            "CLOUDFLARED_IMAGE": cloudflare_mapping.get(
                "CLOUDFLARED_IMAGE", "cloudflare/cloudflared:latest"
            ),
            "PORTAL_INGRESS_NETWORK": config.portal_network,
        }
    )
    return {
        FRIGATE_ENV: frigate_mapping,
        PORTAL_ENV: portal_mapping,
        LAN_ENV: lan_mapping,
        CLOUDFLARE_ENV: cloudflare_mapping,
    }


def write_runtime_files(
    config: InstallConfig, secret_inputs: SecretInputs, lan_key: str
) -> list[Path]:
    if not FRIGATE_CONFIG.is_file():
        raise InstallerError("缺少 Frigate config/config.yml。")
    config_content = FRIGATE_CONFIG.read_text(encoding="utf-8")
    updated_config = update_retention_days(config_content, config.retention_days)

    changed: list[Path] = []
    comments = ("Generated by deploy.py. Do not commit this file.",)
    for path, mapping in build_env_mappings(config, lan_key).items():
        if path == CLOUDFLARE_ENV and config.provider != "cloudflare":
            continue
        if atomic_write(path, render_env(mapping, comments), 0o600):
            changed.append(path)

    if secret_inputs.camera_password is not None:
        encoded_password = urllib.parse.quote(secret_inputs.camera_password, safe="")
        atomic_write_secret(CAMERA_PASSWORD_FILE, encoded_password)
        changed.append(CAMERA_PASSWORD_FILE)
    if config.provider == "cloudflare" and secret_inputs.cloudflare_token is not None:
        atomic_write_secret(CLOUDFLARE_TOKEN_FILE, secret_inputs.cloudflare_token)
        changed.append(CLOUDFLARE_TOKEN_FILE)

    if not CAMERA_PASSWORD_FILE.is_file() or CAMERA_PASSWORD_FILE.stat().st_size == 0:
        raise InstallerError("摄像头密码文件未创建。")
    if config.provider == "cloudflare" and (
        not CLOUDFLARE_TOKEN_FILE.is_file()
        or CLOUDFLARE_TOKEN_FILE.stat().st_size == 0
    ):
        raise InstallerError("Cloudflare token 文件未创建。")

    if atomic_write(FRIGATE_CONFIG, updated_config, 0o640):
        changed.append(FRIGATE_CONFIG)
    return changed


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(command))
    return subprocess.run(
        command,
        cwd=cwd,
        check=check,
        capture_output=capture,
        text=True,
    )


def host_owns_ip(address: str) -> bool:
    try:
        result = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return any(
        token.split("/", 1)[0] == address
        for line in result.stdout.splitlines()
        for token in line.split()
        if "/" in token
    )


def nearest_mount(path: Path) -> Path:
    candidate = path.resolve(strict=False)
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    for parent in (candidate, *candidate.parents):
        if os.path.ismount(parent):
            return parent
    return Path("/")


def preflight(config: InstallConfig) -> None:
    if sys.platform != "linux":
        raise InstallerError("实际部署只支持 Linux；可在其他系统使用 --dry-run。")
    if os.geteuid() != 0:
        raise InstallerError("实际部署需要 root，请运行：sudo ./install.sh")
    for command in ("docker", "ip"):
        if not command_exists(command):
            raise InstallerError(f"缺少命令：{command}")
    run_command(["docker", "compose", "version"], capture=True)
    run_command(["docker", "info"], capture=True)
    if not host_owns_ip(config.lan_ip):
        raise InstallerError(f"服务器没有绑定局域网地址 {config.lan_ip}。")
    if not Path(config.render_device).exists():
        raise InstallerError(
            f"找不到 {config.render_device}。首版安装器只自动部署 Intel VAAPI。"
        )
    media_parts = config.media_path.parts
    if len(media_parts) > 1 and media_parts[1] in {"media", "mnt"}:
        mount = nearest_mount(Path(config.media_path))
        if mount == Path("/"):
            raise InstallerError(
                f"{config.media_path} 位于 /{media_parts[1]}，但没有检测到独立挂载。"
                "为避免写满系统盘，请先挂载硬盘/NAS，或把本地录像放到 /srv 下。"
            )
    if config.provider in ("cloudflare", "tailscale") and config.configure_tailscale:
        if not command_exists("tailscale"):
            raise InstallerError("选择了自动配置 Tailscale，但系统找不到 tailscale。")


def prepare_directories(config: InstallConfig) -> None:
    media_path = Path(config.media_path)
    if not media_path.exists():
        if not config.create_media_path:
            raise InstallerError(f"录像目录不存在：{config.media_path}")
        media_path.mkdir(parents=True, mode=0o755)
    if not media_path.is_dir():
        raise InstallerError(f"录像路径不是目录：{config.media_path}")

    portal_data = PORTAL_DIR / "data"
    portal_data.mkdir(parents=True, exist_ok=True)
    try:
        os.chown(portal_data, 65532, 65532)
        state_file = portal_data / "state.json"
        if state_file.exists():
            os.chown(state_file, 65532, 65532)
    except OSError as error:
        raise InstallerError(f"无法设置 Portal 数据目录所有权：{error}") from error


def ensure_network(name: str) -> None:
    inspected = subprocess.run(
        ["docker", "network", "inspect", name],
        capture_output=True,
        text=True,
    )
    if inspected.returncode != 0:
        run_command(["docker", "network", "create", name])


def compose(directory: Path, *arguments: str) -> None:
    run_command(
        ["docker", "compose", "--project-directory", str(directory), *arguments]
    )


def configure_tailscale(config: InstallConfig) -> None:
    if not config.configure_tailscale or config.provider == "lan":
        return
    run_command(
        [
            "tailscale",
            "serve",
            "--bg",
            "--https=9443",
            f"http://127.0.0.1:{config.admin_loopback_port}",
        ]
    )
    if config.provider == "tailscale":
        run_command(
            [
                "tailscale",
                "funnel",
                "--bg",
                "--https=8443",
                f"http://127.0.0.1:{config.public_loopback_port}",
            ]
        )


def wait_for_http(url: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        try:
            with direct_opener.open(url, timeout=3) as response:
                if 200 <= response.status < 300:
                    print(f"健康检查通过：{url}")
                    return
                last_error = f"HTTP {response.status}"
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = str(error)
        time.sleep(2)
    raise InstallerError(f"等待健康检查超时：{url}（{last_error}）")


def deploy(config: InstallConfig) -> None:
    prepare_directories(config)
    ensure_network(config.portal_network)

    compose(FRIGATE_DIR, "config", "--quiet")
    compose(PORTAL_DIR, "config", "--quiet")
    compose(LAN_DIR, "config", "--quiet")
    if config.provider == "cloudflare":
        compose(CLOUDFLARE_DIR, "config", "--quiet")

    compose(FRIGATE_DIR, "pull")
    compose(FRIGATE_DIR, "up", "-d")
    compose(PORTAL_DIR, "up", "-d", "--build")
    compose(LAN_DIR, "up", "-d", "--build")
    if config.provider == "cloudflare":
        compose(CLOUDFLARE_DIR, "pull")
        compose(CLOUDFLARE_DIR, "up", "-d")

    configure_tailscale(config)
    wait_for_http(
        f"http://127.0.0.1:{config.public_loopback_port}/healthz", timeout=90
    )
    wait_for_http(f"http://{config.lan_ip}:{config.lan_port}/healthz", timeout=60)


def final_instructions(config: InstallConfig) -> None:
    print("\n=== 完成 ===")
    print(f"局域网观看：http://{config.lan_ip}:{config.lan_port}/")
    print(
        f"Frigate 管理：https://{config.frigate_admin_ip}:"
        f"{config.frigate_admin_port}/"
    )
    if config.provider == "cloudflare":
        print(f"客户公网：{config.public_base_url}/")
        print(
            "首次部署请确认 Cloudflare Tunnel 的 Published application 源站为 "
            "http://camera-portal:8080。"
        )
        print(f"设备管理：{config.admin_origin}/")
    elif config.provider == "tailscale":
        print(f"客户公网：{config.public_base_url}/")
        print(f"设备管理：{config.admin_origin}/")
    else:
        print("当前仅启用局域网；以后重新运行脚本即可增加公网和管理入口。")
    if config.provider != "lan" and not config.configure_tailscale:
        print("本次未修改 Tailscale；请按 README 手动配置管理入口/Funnel。")
    print("脚本没有修改路由器、DHCP、DNS、Mihomo、防火墙或录像历史。")


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        if not arguments.configure_only and not arguments.dry_run:
            if sys.platform != "linux":
                raise InstallerError(
                    "实际部署只支持 Linux；可在其他系统使用 --dry-run。"
                )
            if os.geteuid() != 0:
                raise InstallerError("实际部署需要 root，请运行：sudo ./install.sh")

        config, secret_inputs, lan_key = collect_answers(
            prompt_secrets=not arguments.dry_run,
            check_host_paths=not arguments.dry_run,
        )
        print(config_summary(config))
        if arguments.dry_run:
            print("\nDry-run 完成：没有写文件，也没有启动服务。")
            return 0
        if not arguments.yes and not prompt_yes_no(
            "确认写入配置"
            + ("并开始部署" if not arguments.configure_only else ""),
            default=False,
        ):
            print("已取消，没有写文件。")
            return 0

        if not arguments.configure_only:
            preflight(config)

        changed = write_runtime_files(config, secret_inputs, lan_key)
        print("\n已生成/更新：")
        if changed:
            for path in changed:
                print(f"  - {path.relative_to(PROJECT_ROOT)}")
        else:
            print("  - 配置没有变化")

        if arguments.configure_only:
            print("\n配置已生成，未启动 Docker/Tailscale。")
            return 0

        deploy(config)
        final_instructions(config)
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\n已取消。", file=sys.stderr)
        return 130
    except (InstallerError, OSError, subprocess.CalledProcessError) as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
