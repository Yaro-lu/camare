"""Read-only runtime verification for a rendered Camare deployment."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Literal, Protocol

from .checks import CommandResult, CommandRunner, LocalCommandRunner
from .model import DeploymentConfig


VerificationLevel = Literal["PASS", "WARN", "FAIL"]


@dataclass(frozen=True)
class VerificationResult:
    level: VerificationLevel
    code: str
    message: str
    detail: str = ""


@dataclass(frozen=True)
class HTTPResult:
    status_code: int


@dataclass(frozen=True)
class RecordingSnapshot:
    identity: str
    mtime_ns: int
    size: int


class HTTPProbe(Protocol):
    def get(self, url: str, timeout: float) -> HTTPResult: ...


class PathProbe(Protocol):
    def read_text(self, path: Path, max_bytes: int) -> str: ...

    def sha256_regular_file(self, path: Path, max_bytes: int) -> str | None: ...

    def latest_recording(
        self,
        recordings_root: Path,
        stream_id: str,
    ) -> RecordingSnapshot | None: ...


class Sleeper(Protocol):
    def sleep(self, seconds: float) -> None: ...


class LocalHTTPProbe:
    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
        )

    def get(self, url: str, timeout: float) -> HTTPResult:
        request = urllib.request.Request(url, method="GET")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return HTTPResult(status_code=response.status)
        except urllib.error.HTTPError as error:
            return HTTPResult(status_code=error.code)
        except (OSError, ValueError):
            return HTTPResult(status_code=0)


class LocalPathProbe:
    def read_text(self, path: Path, max_bytes: int) -> str:
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode) or path.is_symlink():
            raise ValueError("verification input must be a regular non-symlink file")
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("file exceeds verification limit")
        return data.decode("utf-8")

    def sha256_regular_file(self, path: Path, max_bytes: int) -> str | None:
        try:
            status = path.lstat()
        except OSError:
            return None
        if (
            not stat.S_ISREG(status.st_mode)
            or path.is_symlink()
            or status.st_size > max_bytes
        ):
            return None
        digest = hashlib.sha256()
        total = 0
        try:
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(65536), b""):
                    total += len(block)
                    if total > max_bytes:
                        return None
                    digest.update(block)
        except OSError:
            return None
        return digest.hexdigest()

    def latest_recording(
        self,
        recordings_root: Path,
        stream_id: str,
    ) -> RecordingSnapshot | None:
        latest: tuple[int, str, int] | None = None
        for candidate in recordings_root.rglob(f"{stream_id}/*.mp4"):
            try:
                status = candidate.stat()
            except OSError:
                continue
            if not candidate.is_file():
                continue
            snapshot = (status.st_mtime_ns, str(candidate), status.st_size)
            if latest is None or snapshot[:2] > latest[:2]:
                latest = snapshot
        if latest is None:
            return None
        return RecordingSnapshot(
            identity=latest[1],
            mtime_ns=latest[0],
            size=latest[2],
        )


class LocalSleeper:
    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


MANIFEST_MAX_BYTES = 1 << 20
COMPOSE_MAX_BYTES = 4 << 20
API_MAX_BYTES = 4 << 20
RECORDING_WAIT_SECONDS = 12.0
COMMAND_TIMEOUT_SECONDS = 10.0
HTTP_TIMEOUT_SECONDS = 5.0


def _enabled_stream_ids(config: DeploymentConfig) -> tuple[str, ...]:
    return tuple(
        stream.id
        for group in config.groups
        if group.enabled
        for stream in group.streams
    )


def _manifest_compose_files(
    config: DeploymentConfig,
    runtime_root: Path,
    paths: PathProbe,
) -> tuple[Path, ...]:
    manifest_text = paths.read_text(
        runtime_root / "manifest.json",
        MANIFEST_MAX_BYTES,
    )
    manifest = json.loads(manifest_text)
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    if manifest.get("format_version") != 1:
        raise ValueError("unsupported manifest version")
    if manifest.get("instance_id") != config.instance_id:
        raise ValueError("manifest instance mismatch")
    if manifest.get("provider") != config.provider:
        raise ValueError("manifest provider mismatch")
    values = manifest.get("compose_files")
    if not isinstance(values, list) or not values:
        raise ValueError("manifest has no compose files")
    file_digests = manifest.get("files")
    if not isinstance(file_digests, dict):
        raise ValueError("manifest has no file digests")
    expected_files = {
        ".env",
        "compose.yaml",
        "frigate/config.yml",
        "caddy/Caddyfile",
    }
    if config.hardware.mode == "intel-vaapi":
        expected_files.add("compose.intel.yaml")
    if set(file_digests) != expected_files:
        raise ValueError("manifest file inventory is incomplete or unexpected")

    verified_files: dict[str, Path] = {}
    verified_paths: set[Path] = set()
    for value, expected_digest in file_digests.items():
        if not isinstance(value, str) or not value or "\\" in value:
            raise ValueError("invalid manifest file")
        relative = PurePosixPath(value)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() != value
        ):
            raise ValueError("manifest file escapes runtime")
        if not isinstance(expected_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_digest
        ):
            raise ValueError("manifest file has no valid digest")
        verified_file = runtime_root.joinpath(*relative.parts)
        if verified_file in verified_paths:
            raise ValueError("manifest contains duplicate file paths")
        verified_paths.add(verified_file)
        actual_digest = paths.sha256_regular_file(
            verified_file,
            COMPOSE_MAX_BYTES,
        )
        if actual_digest != expected_digest:
            raise ValueError("manifest file is missing, unsafe, or modified")
        verified_files[value] = verified_file

    expected_compose_files = ["compose.yaml"]
    if config.hardware.mode == "intel-vaapi":
        expected_compose_files.append("compose.intel.yaml")
    if values != expected_compose_files:
        raise ValueError("manifest compose file list does not match hardware mode")
    compose_files = [verified_files[value] for value in values]
    return tuple(compose_files)


def _compose_prefix(
    config: DeploymentConfig,
    runtime_root: Path,
    compose_files: tuple[Path, ...],
) -> tuple[str, ...]:
    command = [
        "docker",
        "compose",
        "--project-directory",
        str(runtime_root),
        "--project-name",
        f"camare_{config.instance_id}",
        "--env-file",
        str(runtime_root / ".env"),
    ]
    for compose_file in compose_files:
        command.extend(("-f", str(compose_file)))
    return tuple(command)


def _json_value(output: str) -> object:
    if len(output.encode("utf-8")) > API_MAX_BYTES:
        raise ValueError("response exceeds verification limit")
    return json.loads(output)


def _compose_rows(output: str) -> tuple[dict[str, object], ...]:
    try:
        value = _json_value(output)
    except (UnicodeError, ValueError, TypeError):
        rows: list[dict[str, object]] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("compose row is not an object")
            rows.append(item)
        if not rows:
            raise ValueError("compose output is empty")
        return tuple(rows)
    if isinstance(value, dict):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return tuple(value)
    raise ValueError("compose output is not a row list")


def _field(row: dict[str, object], name: str) -> str:
    value = row.get(name, row.get(name.lower(), ""))
    return value.strip().lower() if isinstance(value, str) else ""


def _service_results(
    config: DeploymentConfig,
    rows: tuple[dict[str, object], ...],
) -> tuple[tuple[VerificationResult, ...], bool]:
    expected = ["frigate", "camera-portal", "lan-caddy"]
    if config.provider == "cloudflare":
        expected.append("cloudflared")
    by_service: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        service = _field(row, "Service")
        if service:
            by_service.setdefault(service, []).append(row)

    results: list[VerificationResult] = []
    frigate_ready = False
    health_required = {"camera-portal", "lan-caddy"}
    for service in expected:
        matches = by_service.get(service, [])
        if len(matches) != 1:
            results.append(
                VerificationResult(
                    "FAIL",
                    f"compose.service.{service}",
                    "Compose service is missing or duplicated",
                )
            )
            continue
        state = _field(matches[0], "State")
        health = _field(matches[0], "Health")
        healthy = state == "running" and (
            health == "healthy"
            or (not health and service not in health_required)
        )
        results.append(
            VerificationResult(
                "PASS" if healthy else "FAIL",
                f"compose.service.{service}",
                "Compose service is running and healthy"
                if healthy
                else "Compose service is not ready",
            )
        )
        if service == "frigate":
            frigate_ready = healthy

    unexpected = set(by_service).difference(expected)
    if unexpected:
        results.append(
            VerificationResult(
                "FAIL",
                "compose.services.unexpected",
                "Unexpected services exist in this Compose project",
                f"count: {len(unexpected)}",
            )
        )
    return tuple(results), frigate_ready


def _positive_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and value > 0
    )


def _stats_results(
    output: str,
    stream_ids: tuple[str, ...],
) -> tuple[VerificationResult, ...]:
    try:
        value = _json_value(output)
    except (UnicodeError, ValueError, TypeError):
        return (
            VerificationResult(
                "FAIL",
                "frigate.stats",
                "Frigate stats returned malformed JSON",
            ),
        )
    cameras = value.get("cameras") if isinstance(value, dict) else None
    if not isinstance(cameras, dict):
        return (
            VerificationResult(
                "FAIL",
                "frigate.stats",
                "Frigate stats has no camera map",
            ),
        )
    required_fields = (
        "pid",
        "capture_pid",
        "ffmpeg_pid",
        "camera_fps",
        "process_fps",
    )
    results: list[VerificationResult] = []
    for stream_id in stream_ids:
        camera = cameras.get(stream_id)
        ready = isinstance(camera, dict) and all(
            _positive_number(camera.get(field))
            for field in required_fields
        )
        results.append(
            VerificationResult(
                "PASS" if ready else "FAIL",
                f"frigate.stats.{stream_id}",
                "Frigate capture processes and frame rates are active"
                if ready
                else "Frigate capture process or frame rate is inactive",
            )
        )
    return tuple(results)


def _producer_results(
    output: str,
    stream_ids: tuple[str, ...],
) -> tuple[VerificationResult, ...]:
    try:
        value = _json_value(output)
    except (UnicodeError, ValueError, TypeError):
        return (
            VerificationResult(
                "FAIL",
                "go2rtc.streams",
                "go2rtc streams returned malformed JSON",
            ),
        )
    if not isinstance(value, dict):
        return (
            VerificationResult(
                "FAIL",
                "go2rtc.streams",
                "go2rtc streams has no stream map",
            ),
        )
    results: list[VerificationResult] = []
    for stream_id in stream_ids:
        stream = value.get(stream_id)
        producers = stream.get("producers") if isinstance(stream, dict) else None
        ready = isinstance(producers, list) and len(producers) >= 1
        results.append(
            VerificationResult(
                "PASS" if ready else "FAIL",
                f"go2rtc.producer.{stream_id}",
                "go2rtc has an upstream producer"
                if ready
                else "go2rtc has no upstream producer",
                f"producer count: {len(producers)}"
                if isinstance(producers, list)
                else "producer count: 0",
            )
        )
    return tuple(results)


def _recording_advanced(
    before: RecordingSnapshot | None,
    after: RecordingSnapshot | None,
) -> bool:
    if after is None:
        return False
    if before is None:
        return True
    return (
        after.identity != before.identity
        or after.mtime_ns > before.mtime_ns
        or after.size > before.size
    )


def _sleep(sleeper: Sleeper | Callable[[float], None], seconds: float) -> None:
    if callable(sleeper):
        sleeper(seconds)
    else:
        sleeper.sleep(seconds)


def _recording_results(
    config: DeploymentConfig,
    stream_ids: tuple[str, ...],
    paths: PathProbe,
    sleeper: Sleeper | Callable[[float], None],
) -> tuple[VerificationResult, ...]:
    root = Path(config.media) / "recordings"
    before: dict[str, RecordingSnapshot | None] = {}
    failed: set[str] = set()
    for stream_id in stream_ids:
        try:
            before[stream_id] = paths.latest_recording(root, stream_id)
        except OSError:
            failed.add(stream_id)
    _sleep(sleeper, RECORDING_WAIT_SECONDS)
    results: list[VerificationResult] = []
    for stream_id in stream_ids:
        try:
            after = paths.latest_recording(root, stream_id)
        except OSError:
            after = None
            failed.add(stream_id)
        advanced = stream_id not in failed and _recording_advanced(
            before.get(stream_id),
            after,
        )
        results.append(
            VerificationResult(
                "PASS" if advanced else "FAIL",
                f"recording.{stream_id}",
                "Recording MP4 advanced during the bounded observation"
                if advanced
                else "Recording MP4 did not advance during the bounded observation",
            )
        )
    return tuple(results)


def _skipped_recording_results(
    stream_ids: tuple[str, ...],
) -> tuple[VerificationResult, ...]:
    return tuple(
        VerificationResult(
            "FAIL",
            f"recording.{stream_id}",
            "Recording progress check was skipped because the media pipeline is not ready",
        )
        for stream_id in stream_ids
    )


def _http_results(config: DeploymentConfig, http: HTTPProbe) -> tuple[VerificationResult, ...]:
    endpoints = (
        (
            "http.portal.public",
            f"http://127.0.0.1:{config.public.loopback_port}/healthz",
        ),
        (
            "http.portal.admin",
            f"http://127.0.0.1:{config.admin.loopback_port}/healthz",
        ),
        (
            "http.lan",
            f"http://{config.lan.bind_ip}:{config.lan.port}/healthz",
        ),
    )
    results: list[VerificationResult] = []
    for code, url in endpoints:
        try:
            response = http.get(url, timeout=HTTP_TIMEOUT_SECONDS)
            status = response.status_code
        except OSError:
            status = 0
        ready = status == 200
        results.append(
            VerificationResult(
                "PASS" if ready else "FAIL",
                code,
                "HTTP health endpoint is ready"
                if ready
                else "HTTP health endpoint is unavailable",
                f"status: {status}",
            )
        )
    return tuple(results)


def verify_runtime(
    config: DeploymentConfig,
    runtime_root: Path,
    *,
    runner: CommandRunner | None = None,
    http: HTTPProbe | None = None,
    path: PathProbe | None = None,
    sleeper: Sleeper | Callable[[float], None] | None = None,
) -> tuple[VerificationResult, ...]:
    """Verify one rendered runtime without changing configuration or containers."""

    command_runner = runner or LocalCommandRunner()
    http_probe = http or LocalHTTPProbe()
    paths = path or LocalPathProbe()
    wait = sleeper or LocalSleeper()
    root = Path(runtime_root)
    results: list[VerificationResult] = []

    try:
        compose_files = _manifest_compose_files(config, root, paths)
    except (OSError, UnicodeError, ValueError, TypeError):
        return (
            VerificationResult(
                "FAIL",
                "runtime.manifest",
                "Runtime manifest is missing, malformed, or belongs to another instance",
            ),
        )
    results.append(
        VerificationResult(
            "PASS",
            "runtime.manifest",
            "Runtime manifest is valid",
            f"compose files: {len(compose_files)}",
        )
    )

    prefix = _compose_prefix(config, root, compose_files)
    compose_ps = command_runner.run(
        prefix + ("ps", "--all", "--format", "json"),
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    frigate_ready = False
    compose_ready = False
    if compose_ps.returncode != 0:
        results.append(
            VerificationResult(
                "FAIL",
                "compose.ps",
                "Compose service state query failed",
            )
        )
    else:
        try:
            rows = _compose_rows(compose_ps.stdout)
        except (UnicodeError, ValueError, TypeError):
            results.append(
                VerificationResult(
                    "FAIL",
                    "compose.ps",
                    "Compose service state returned malformed JSON",
                )
            )
        else:
            results.append(
                VerificationResult(
                    "PASS",
                    "compose.ps",
                    "Compose service state is readable",
                )
            )
            service_results, frigate_ready = _service_results(config, rows)
            results.extend(service_results)
            compose_ready = all(
                result.level == "PASS" for result in service_results
            )

    stream_ids = _enabled_stream_ids(config)
    media_checks_ready = compose_ready and frigate_ready
    if frigate_ready:
        for endpoint, parser, failure_code, failure_message in (
            (
                "http://127.0.0.1:5000/api/stats",
                _stats_results,
                "frigate.stats",
                "Frigate stats request failed",
            ),
            (
                "http://127.0.0.1:1984/api/streams",
                _producer_results,
                "go2rtc.streams",
                "go2rtc streams request failed",
            ),
            ):
            response: CommandResult = command_runner.run(
                prefix
                + (
                    "exec",
                    "-T",
                    "frigate",
                    "wget",
                    "-qO-",
                    endpoint,
                ),
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
            if response.returncode == 0:
                parsed_results = parser(response.stdout, stream_ids)
                results.extend(parsed_results)
                media_checks_ready = media_checks_ready and all(
                    result.level == "PASS" for result in parsed_results
                )
            else:
                media_checks_ready = False
                results.append(
                    VerificationResult(
                        "FAIL",
                        failure_code,
                        failure_message,
                    )
                )
    else:
        results.extend(
            (
                VerificationResult(
                    "FAIL",
                    "frigate.stats",
                    "Frigate stats check was unavailable because the service is not ready",
                ),
                VerificationResult(
                    "FAIL",
                    "go2rtc.streams",
                    "go2rtc producer check was unavailable because Frigate is not ready",
                ),
            )
        )

    if media_checks_ready:
        results.extend(_recording_results(config, stream_ids, paths, wait))
    else:
        results.extend(_skipped_recording_results(stream_ids))
    results.extend(_http_results(config, http_probe))
    return tuple(results)
