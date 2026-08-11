"""Read-only host checks for a Camare deployment."""

from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from .model import DeploymentConfig


CheckLevel = Literal["PASS", "WARN", "FAIL"]


@dataclass(frozen=True)
class CheckResult:
    level: CheckLevel
    code: str
    message: str
    detail: str = ""


@dataclass(frozen=True)
class PlatformSnapshot:
    system: str
    machine: str
    python_version: tuple[int, int, int]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""


@dataclass(frozen=True)
class PathStatus:
    exists: bool
    is_dir: bool = False
    is_file: bool = False
    is_char_device: bool = False
    is_root: bool = False
    readable: bool = False
    writable: bool = False
    free_bytes: int | None = None


class PlatformProbe(Protocol):
    def snapshot(self) -> PlatformSnapshot: ...


class CommandRunner(Protocol):
    def run(self, command: tuple[str, ...], timeout: float) -> CommandResult: ...


class PathProbe(Protocol):
    def inspect(self, path: PurePosixPath) -> PathStatus: ...


class SocketProbe(Protocol):
    def can_bind(self, host: str, port: int) -> bool: ...

    def can_connect(self, host: str, port: int, timeout: float) -> bool: ...


class LocalPlatformProbe:
    def snapshot(self) -> PlatformSnapshot:
        return PlatformSnapshot(
            system=platform.system(),
            machine=platform.machine(),
            python_version=(
                sys.version_info.major,
                sys.version_info.minor,
                sys.version_info.micro,
            ),
        )


class LocalCommandRunner:
    def run(self, command: tuple[str, ...], timeout: float) -> CommandResult:
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            return CommandResult(returncode=127)
        except subprocess.TimeoutExpired:
            return CommandResult(returncode=124)
        return CommandResult(returncode=completed.returncode, stdout=completed.stdout)


class LocalPathProbe:
    def inspect(self, path: PurePosixPath) -> PathStatus:
        candidate = Path(str(path))
        try:
            path_stat = candidate.stat()
        except OSError:
            return PathStatus(exists=False)
        is_directory = stat.S_ISDIR(path_stat.st_mode)
        try:
            is_root = candidate.resolve(strict=False) == Path(candidate.anchor)
        except OSError:
            is_root = False
        return PathStatus(
            exists=True,
            is_dir=is_directory,
            is_file=stat.S_ISREG(path_stat.st_mode),
            is_char_device=stat.S_ISCHR(path_stat.st_mode),
            is_root=is_root,
            readable=os.access(candidate, os.R_OK),
            writable=os.access(
                candidate,
                os.W_OK | (os.X_OK if is_directory else 0),
            ),
            free_bytes=shutil.disk_usage(candidate).free if is_directory else None,
        )


class LocalSocketProbe:
    def can_bind(self, host: str, port: int) -> bool:
        candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            candidate.bind((host, port))
        except OSError:
            return False
        finally:
            candidate.close()
        return True

    def can_connect(self, host: str, port: int, timeout: float) -> bool:
        try:
            connection = socket.create_connection((host, port), timeout=timeout)
        except OSError:
            return False
        connection.close()
        return True


MEDIA_FREE_WARN_BYTES = 10 * 1024**3
MEDIA_FREE_FAIL_BYTES = 1 * 1024**3


def run_doctor(
    config: DeploymentConfig,
    *,
    runner: CommandRunner | None = None,
    socket_probe: SocketProbe | None = None,
    path_probe: PathProbe | None = None,
    platform_probe: PlatformProbe | None = None,
) -> tuple[CheckResult, ...]:
    """Return host readiness results without changing host state."""

    snapshot = (platform_probe or LocalPlatformProbe()).snapshot()
    command_runner = runner or LocalCommandRunner()
    paths = path_probe or LocalPathProbe()
    sockets = socket_probe or LocalSocketProbe()
    results: list[CheckResult] = []

    if snapshot.system.lower() == "linux":
        results.append(CheckResult("PASS", "platform.os", "Operating system is Linux"))
    else:
        results.append(CheckResult("FAIL", "platform.os", "Deployment requires Linux"))

    if snapshot.machine.lower() in {"x86_64", "amd64"}:
        results.append(CheckResult("PASS", "platform.arch", "Architecture is x86-64"))
    else:
        results.append(CheckResult("FAIL", "platform.arch", "Deployment requires x86-64"))

    version = snapshot.python_version
    if version >= (3, 10, 0):
        results.append(CheckResult("PASS", "python.version", "Python is supported"))
    else:
        results.append(CheckResult("FAIL", "python.version", "Python 3.10 or newer is required"))

    docker = command_runner.run(
        ("docker", "info", "--format", "{{.ServerVersion}}"),
        timeout=5,
    )
    if docker.returncode == 0 and docker.stdout.strip():
        results.append(
            CheckResult(
                "PASS",
                "docker.engine",
                "Docker engine is reachable",
                docker.stdout.strip().splitlines()[0],
            )
        )
    else:
        results.append(CheckResult("FAIL", "docker.engine", "Docker engine is unavailable"))

    compose = command_runner.run(
        ("docker", "compose", "version", "--short"),
        timeout=5,
    )
    compose_match = re.search(r"(?:^|\D)(\d+)(?:\.\d+)", compose.stdout.strip())
    if compose.returncode == 0 and compose_match and int(compose_match.group(1)) >= 2:
        results.append(
            CheckResult(
                "PASS",
                "docker.compose",
                "Docker Compose v2 is available",
                compose.stdout.strip().splitlines()[0],
            )
        )
    else:
        results.append(CheckResult("FAIL", "docker.compose", "Docker Compose v2 is required"))

    compose_up_help = command_runner.run(
        ("docker", "compose", "up", "--help"),
        timeout=5,
    )
    compose_wait_available = (
        compose_up_help.returncode == 0
        and re.search(
            r"(?m)^\s*--wait(?:\s|$)",
            compose_up_help.stdout,
        )
        is not None
    )
    results.append(
        CheckResult(
            "PASS" if compose_wait_available else "FAIL",
            "docker.compose.wait",
            (
                "Docker Compose can wait for service health"
                if compose_wait_available
                else "Docker Compose up --wait is required"
            ),
        )
    )

    timezone_path = PurePosixPath("/usr/share/zoneinfo") / config.timezone
    timezone_status = paths.inspect(timezone_path)
    if timezone_status.exists and timezone_status.is_file and timezone_status.readable:
        results.append(CheckResult("PASS", "timezone.file", "Timezone data is available"))
    else:
        results.append(
            CheckResult(
                "FAIL",
                "timezone.file",
                "Configured timezone data is unavailable",
                str(timezone_path),
            )
        )

    runtime_status = paths.inspect(config.runtime_dir)
    if runtime_status.exists:
        runtime_ready = runtime_status.is_dir and runtime_status.writable
        runtime_detail = str(config.runtime_dir)
    else:
        parent = config.runtime_dir.parent
        parent_status = paths.inspect(parent)
        while not parent_status.exists and parent != parent.parent:
            parent = parent.parent
            parent_status = paths.inspect(parent)
        runtime_ready = parent_status.exists and parent_status.is_dir and parent_status.writable
        runtime_detail = f"writable parent: {parent}"
    results.append(
        CheckResult(
            "PASS" if runtime_ready else "FAIL",
            "runtime.path",
            "Runtime path is writable" if runtime_ready else "Runtime path has no writable directory or parent",
            runtime_detail,
        )
    )

    media_status = paths.inspect(config.media)
    media_ready = (
        config.media != PurePosixPath("/")
        and not media_status.is_root
        and media_status.exists
        and media_status.is_dir
        and media_status.readable
        and media_status.writable
    )
    results.append(
        CheckResult(
            "PASS" if media_ready else "FAIL",
            "media.path",
            "Media directory is ready" if media_ready else "Media path must be an existing writable directory outside root",
            str(config.media),
        )
    )
    if not media_ready or media_status.free_bytes is None:
        results.append(
            CheckResult(
                "FAIL" if not media_ready else "WARN",
                "media.space",
                "Media free space is unavailable",
            )
        )
    elif media_status.free_bytes < MEDIA_FREE_FAIL_BYTES:
        results.append(CheckResult("FAIL", "media.space", "Media free space is critically low"))
    elif media_status.free_bytes < MEDIA_FREE_WARN_BYTES:
        results.append(CheckResult("WARN", "media.space", "Media free space is low"))
    else:
        results.append(CheckResult("PASS", "media.space", "Media free space is sufficient"))

    if config.hardware.mode == "cpu":
        results.append(
            CheckResult(
                "PASS",
                "hardware.vaapi",
                "CPU mode selected; VAAPI device check was skipped",
            )
        )
    else:
        render_device = config.hardware.render_device
        render_status = paths.inspect(render_device) if render_device is not None else PathStatus(False)
        render_ready = (
            render_device is not None
            and render_status.exists
            and render_status.is_char_device
            and render_status.readable
            and render_status.writable
        )
        results.append(
            CheckResult(
                "PASS" if render_ready else "FAIL",
                "hardware.vaapi",
                "Intel VAAPI render device is accessible" if render_ready else "Intel VAAPI render device must be an accessible character device",
                str(render_device) if render_device is not None else "",
            )
        )

    endpoints = (
        ("public", "127.0.0.1", config.public.loopback_port),
        ("admin", "127.0.0.1", config.admin.loopback_port),
        ("lan", config.lan.bind_ip, config.lan.port),
        ("frigate", config.admin.frigate_bind_ip, config.admin.frigate_port),
    )
    endpoint_counts: dict[tuple[str, int], int] = {}
    for _, host, port in endpoints:
        endpoint_counts[(host, port)] = endpoint_counts.get((host, port), 0) + 1
    for name, host, port in endpoints:
        endpoint = (host, port)
        if endpoint_counts[endpoint] > 1:
            available = False
            message = "Configured port conflicts with another Camare endpoint"
        else:
            available = sockets.can_bind(host, port)
            message = "Configured port is available" if available else "Configured port is already in use or cannot be bound"
        results.append(
            CheckResult(
                "PASS" if available else "FAIL",
                f"port.{name}",
                message,
                f"{host}:{port}",
            )
        )

    enabled_groups = tuple(group for group in config.groups if group.enabled)
    reachable_groups: set[str] = set()
    for group in enabled_groups:
        reachable = sockets.can_connect(group.host, group.rtsp_port, timeout=3)
        if reachable:
            reachable_groups.add(group.id)
        results.append(
            CheckResult(
                "PASS" if reachable else "FAIL",
                f"camera.tcp.{group.id}",
                "Camera RTSP port is reachable" if reachable else "Camera RTSP port is unreachable",
                f"{group.host}:{group.rtsp_port}",
            )
        )

    ffprobe = command_runner.run(("ffprobe", "-version"), timeout=5)
    ffprobe_available = ffprobe.returncode == 0
    results.append(
        CheckResult(
            "PASS" if ffprobe_available else "WARN",
            "ffprobe.available",
            "ffprobe is available" if ffprobe_available else "ffprobe is unavailable; H.264 validation was skipped",
        )
    )
    if ffprobe_available:
        for group in enabled_groups:
            if group.id not in reachable_groups:
                continue
            for stream in group.streams:
                stream_url = f"rtsp://{group.host}:{group.rtsp_port}{stream.path}"
                probe = command_runner.run(
                    (
                        "ffprobe",
                        "-v",
                        "error",
                        "-rtsp_transport",
                        "tcp",
                        "-select_streams",
                        "v:0",
                        "-show_entries",
                        "stream=codec_name",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        stream_url,
                    ),
                    timeout=10,
                )
                codec = next(
                    (line.strip().lower() for line in probe.stdout.splitlines() if line.strip()),
                    "",
                )
                if probe.returncode != 0 or not codec:
                    results.append(
                        CheckResult(
                            "WARN",
                            f"camera.codec.{stream.id}",
                            "Camera codec could not be verified without credentials; verify after deployment",
                        )
                    )
                elif codec == "h264":
                    results.append(
                        CheckResult(
                            "PASS",
                            f"camera.codec.{stream.id}",
                            "Camera stream uses H.264",
                        )
                    )
                else:
                    results.append(
                        CheckResult(
                            "FAIL",
                            f"camera.codec.{stream.id}",
                            "Camera stream is not H.264",
                            f"detected codec: {codec}",
                        )
                    )
    return tuple(results)
