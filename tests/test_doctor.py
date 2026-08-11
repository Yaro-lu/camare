import unittest
from dataclasses import replace
from pathlib import Path, PurePosixPath

from camarectl.checks import CommandResult, PathStatus, PlatformSnapshot, run_doctor
from camarectl.model import (
    AdminConfig,
    DeploymentConfig,
    GroupConfig,
    HardwareConfig,
    LanConfig,
    PublicConfig,
    StreamConfig,
)


COMPOSE_UP_HELP_WITH_WAIT = """Usage: docker compose up [OPTIONS]
      --wait                 Wait for services to be running|healthy
      --wait-timeout int     Maximum duration to wait
"""


def deployment_config() -> DeploymentConfig:
    return DeploymentConfig(
        instance_id="test-camera",
        runtime_dir=PurePosixPath("/var/lib/camare/test-camera"),
        timezone="Asia/Shanghai",
        provider="lan",
        public=PublicConfig(origin="", loopback_port=19080),
        admin=AdminConfig(
            origin="",
            loopback_port=19081,
            tailscale_login="",
            frigate_bind_ip="127.0.0.1",
            frigate_port=18971,
        ),
        lan=LanConfig(bind_ip="192.168.1.250", port=18088),
        media=PurePosixPath("/srv/frigate/test-media"),
        retention=14,
        hardware=HardwareConfig(mode="cpu", render_device=None, va_driver=None),
        groups=(
            GroupConfig(
                id="yard",
                name="Yard",
                host="192.168.1.128",
                rtsp_user="admin",
                rtsp_port=554,
                password_file=Path("C:/sealed/yard.password"),
                streams=(
                    StreamConfig(id="ptz_view", name="PTZ", path="/live/101"),
                    StreamConfig(id="fixed_view", name="Fixed", path="/live/201"),
                ),
            ),
        ),
    )


class FakePlatformProbe:
    def __init__(self, system: str, machine: str, python: tuple[int, int, int]):
        self.snapshot_value = PlatformSnapshot(system, machine, python)

    def snapshot(self) -> PlatformSnapshot:
        return self.snapshot_value


class FakeRunner:
    def __init__(self, responses: dict[tuple[str, ...], CommandResult]):
        self.responses = responses
        self.commands: list[tuple[str, ...]] = []

    def run(self, command: tuple[str, ...], timeout: float) -> CommandResult:
        del timeout
        self.commands.append(command)
        return self.responses.get(command, CommandResult(returncode=127))


class FakePathProbe:
    def __init__(self, statuses: dict[PurePosixPath, PathStatus]):
        self.statuses = statuses
        self.inspected: list[PurePosixPath] = []

    def inspect(self, path: PurePosixPath) -> PathStatus:
        self.inspected.append(path)
        return self.statuses.get(path, PathStatus(exists=False))


class FakeSocketProbe:
    def __init__(
        self,
        *,
        bindable: dict[tuple[str, int], bool] | None = None,
        connectable: dict[tuple[str, int], bool] | None = None,
    ):
        self.bindable = bindable or {}
        self.connectable = connectable or {}
        self.bind_calls: list[tuple[str, int]] = []
        self.connect_calls: list[tuple[str, int]] = []

    def can_bind(self, host: str, port: int) -> bool:
        self.bind_calls.append((host, port))
        return self.bindable.get((host, port), True)

    def can_connect(self, host: str, port: int, timeout: float) -> bool:
        del timeout
        self.connect_calls.append((host, port))
        return self.connectable.get((host, port), True)


def baseline_probes(**overrides: object) -> dict[str, object]:
    gibibyte = 1024**3
    probes: dict[str, object] = {
        "runner": FakeRunner(
            {
                ("docker", "info", "--format", "{{.ServerVersion}}"): CommandResult(0, "28.3.3"),
                ("docker", "compose", "version", "--short"): CommandResult(0, "2.39.1"),
                ("docker", "compose", "up", "--help"): CommandResult(
                    0, COMPOSE_UP_HELP_WITH_WAIT
                ),
                ("ffprobe", "-version"): CommandResult(returncode=127),
            }
        ),
        "socket_probe": FakeSocketProbe(),
        "path_probe": FakePathProbe(
            {
                PurePosixPath("/usr/share/zoneinfo/Asia/Shanghai"): PathStatus(
                    exists=True,
                    is_file=True,
                    readable=True,
                ),
                PurePosixPath("/var/lib/camare/test-camera"): PathStatus(
                    exists=True,
                    is_dir=True,
                    readable=True,
                    writable=True,
                ),
                PurePosixPath("/srv/frigate/test-media"): PathStatus(
                    exists=True,
                    is_dir=True,
                    readable=True,
                    writable=True,
                    free_bytes=50 * gibibyte,
                ),
            }
        ),
        "platform_probe": FakePlatformProbe("Linux", "x86_64", (3, 13, 0)),
    }
    probes.update(overrides)
    return probes


class DoctorTests(unittest.TestCase):
    def test_reports_supported_platform_architecture_and_python(self) -> None:
        results = run_doctor(
            deployment_config(),
            **baseline_probes(
                platform_probe=FakePlatformProbe("Linux", "x86_64", (3, 10, 0)),
            ),
        )
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["platform.os"].level, "PASS")
        self.assertEqual(by_code["platform.arch"].level, "PASS")
        self.assertEqual(by_code["python.version"].level, "PASS")

    def test_requires_a_reachable_docker_engine_and_compose_v2(self) -> None:
        runner = FakeRunner(
            {
                ("docker", "info", "--format", "{{.ServerVersion}}"): CommandResult(
                    returncode=0,
                    stdout="28.3.3\n",
                ),
                ("docker", "compose", "version", "--short"): CommandResult(
                    returncode=0,
                    stdout="2.39.1\n",
                ),
                ("docker", "compose", "up", "--help"): CommandResult(
                    returncode=0,
                    stdout=COMPOSE_UP_HELP_WITH_WAIT,
                ),
            }
        )

        results = run_doctor(
            deployment_config(),
            **baseline_probes(runner=runner),
        )
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["docker.engine"].level, "PASS")
        self.assertEqual(by_code["docker.compose"].level, "PASS")
        self.assertEqual(by_code["docker.compose.wait"].level, "PASS")

    def test_requires_compose_up_wait_capability_without_matching_wait_timeout(self) -> None:
        runner = FakeRunner(
            {
                ("docker", "info", "--format", "{{.ServerVersion}}"): CommandResult(
                    0, "29.5.2\n"
                ),
                ("docker", "compose", "version", "--short"): CommandResult(
                    0, "5.1.4\n"
                ),
                ("docker", "compose", "up", "--help"): CommandResult(
                    0,
                    "      --wait-timeout int   Maximum duration to wait\n",
                ),
            }
        )

        results = run_doctor(
            deployment_config(),
            **baseline_probes(runner=runner),
        )
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["docker.compose"].level, "PASS")
        self.assertEqual(by_code["docker.compose.wait"].level, "FAIL")
        self.assertIn(("docker", "compose", "up", "--help"), runner.commands)

    def test_accepts_a_writable_runtime_parent_and_ready_media_directory(self) -> None:
        gibibyte = 1024**3
        paths = FakePathProbe(
            {
                PurePosixPath("/usr/share/zoneinfo/Asia/Shanghai"): PathStatus(
                    exists=True,
                    is_file=True,
                    readable=True,
                ),
                PurePosixPath("/var/lib/camare/test-camera"): PathStatus(exists=False),
                PurePosixPath("/var/lib/camare"): PathStatus(
                    exists=True,
                    is_dir=True,
                    writable=True,
                ),
                PurePosixPath("/srv/frigate/test-media"): PathStatus(
                    exists=True,
                    is_dir=True,
                    readable=True,
                    writable=True,
                    free_bytes=50 * gibibyte,
                ),
            }
        )

        results = run_doctor(
            deployment_config(),
            **baseline_probes(path_probe=paths),
        )
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["timezone.file"].level, "PASS")
        self.assertEqual(by_code["runtime.path"].level, "PASS")
        self.assertEqual(by_code["media.path"].level, "PASS")
        self.assertEqual(by_code["media.space"].level, "PASS")

    def test_checks_every_configured_host_port_without_listening(self) -> None:
        sockets = FakeSocketProbe()

        results = run_doctor(
            deployment_config(),
            **baseline_probes(socket_probe=sockets),
        )
        by_code = {result.code: result for result in results}

        for code in ("port.public", "port.admin", "port.lan", "port.frigate"):
            self.assertEqual(by_code[code].level, "PASS")
        self.assertEqual(
            sockets.bind_calls,
            [
                ("127.0.0.1", 19080),
                ("127.0.0.1", 19081),
                ("192.168.1.250", 18088),
                ("127.0.0.1", 18971),
            ],
        )

    def test_checks_only_enabled_camera_tcp_and_warns_when_ffprobe_is_missing(self) -> None:
        config = deployment_config()
        disabled_group = replace(
            config.groups[0],
            id="front",
            name="Front",
            host="192.168.1.129",
            enabled=False,
        )
        config = replace(config, groups=(config.groups[0], disabled_group))
        sockets = FakeSocketProbe()
        runner = FakeRunner(
            {
                ("docker", "info", "--format", "{{.ServerVersion}}"): CommandResult(0, "28.3.3"),
                ("docker", "compose", "version", "--short"): CommandResult(0, "2.39.1"),
                ("docker", "compose", "up", "--help"): CommandResult(
                    0, COMPOSE_UP_HELP_WITH_WAIT
                ),
                ("ffprobe", "-version"): CommandResult(returncode=127),
            }
        )

        results = run_doctor(
            config,
            **baseline_probes(runner=runner, socket_probe=sockets),
        )
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["camera.tcp.yard"].level, "PASS")
        self.assertNotIn("camera.tcp.front", by_code)
        self.assertEqual(by_code["ffprobe.available"].level, "WARN")
        self.assertEqual(sockets.connect_calls, [("192.168.1.128", 554)])

    def test_ffprobe_accepts_h264_and_rejects_a_detected_other_codec_without_secrets(self) -> None:
        def ffprobe_command(path: str) -> tuple[str, ...]:
            return (
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
                f"rtsp://192.168.1.128:554{path}",
            )

        runner = FakeRunner(
            {
                ("docker", "info", "--format", "{{.ServerVersion}}"): CommandResult(0, "28.3.3"),
                ("docker", "compose", "version", "--short"): CommandResult(0, "2.39.1"),
                ("docker", "compose", "up", "--help"): CommandResult(
                    0, COMPOSE_UP_HELP_WITH_WAIT
                ),
                ("ffprobe", "-version"): CommandResult(0, "ffprobe 7.1"),
                ffprobe_command("/live/101"): CommandResult(0, "h264\n"),
                ffprobe_command("/live/201"): CommandResult(0, "hevc\n"),
            }
        )

        results = run_doctor(
            deployment_config(),
            **baseline_probes(runner=runner),
        )
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["camera.codec.ptz_view"].level, "PASS")
        self.assertEqual(by_code["camera.codec.fixed_view"].level, "FAIL")
        serialized_commands = repr(runner.commands)
        serialized_results = repr(results)
        self.assertNotIn("yard.password", serialized_commands + serialized_results)
        self.assertNotIn("not-a-real-password", serialized_commands + serialized_results)

    def test_requires_an_accessible_character_device_for_intel_vaapi(self) -> None:
        render_device = PurePosixPath("/dev/dri/renderD128")
        config = replace(
            deployment_config(),
            hardware=HardwareConfig(
                mode="intel-vaapi",
                render_device=render_device,
                va_driver="i965",
            ),
        )
        paths = FakePathProbe(
            {
                render_device: PathStatus(
                    exists=True,
                    is_char_device=True,
                    readable=True,
                    writable=True,
                )
            }
        )

        results = run_doctor(
            config,
            **baseline_probes(path_probe=paths),
        )
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["hardware.vaapi"].level, "PASS")
        self.assertIn(render_device, paths.inspected)

    def test_rejects_a_media_directory_that_resolves_to_the_filesystem_root(self) -> None:
        paths = baseline_probes()["path_probe"]
        self.assertIsInstance(paths, FakePathProbe)
        paths.statuses[PurePosixPath("/srv/frigate/test-media")] = PathStatus(
            exists=True,
            is_dir=True,
            is_root=True,
            readable=True,
            writable=True,
            free_bytes=50 * 1024**3,
        )

        results = run_doctor(
            deployment_config(),
            **baseline_probes(path_probe=paths),
        )
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["media.path"].level, "FAIL")

    def test_fails_unsupported_platform_architecture_and_python(self) -> None:
        results = run_doctor(
            deployment_config(),
            **baseline_probes(
                platform_probe=FakePlatformProbe("Windows", "arm64", (3, 9, 18)),
            ),
        )
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["platform.os"].level, "FAIL")
        self.assertEqual(by_code["platform.arch"].level, "FAIL")
        self.assertEqual(by_code["python.version"].level, "FAIL")

    def test_fails_when_docker_or_compose_v2_is_unavailable(self) -> None:
        runner = FakeRunner(
            {
                ("docker", "info", "--format", "{{.ServerVersion}}"): CommandResult(1),
                ("docker", "compose", "version", "--short"): CommandResult(0, "1.29.2"),
                ("docker", "compose", "up", "--help"): CommandResult(1),
                ("ffprobe", "-version"): CommandResult(127),
            }
        )

        results = run_doctor(
            deployment_config(),
            **baseline_probes(runner=runner),
        )
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["docker.engine"].level, "FAIL")
        self.assertEqual(by_code["docker.compose"].level, "FAIL")
        self.assertEqual(by_code["docker.compose.wait"].level, "FAIL")

    def test_fails_missing_timezone_unwritable_runtime_and_invalid_media(self) -> None:
        paths = FakePathProbe(
            {
                PurePosixPath("/var/lib/camare/test-camera"): PathStatus(exists=False),
                PurePosixPath("/var/lib/camare"): PathStatus(
                    exists=True,
                    is_dir=True,
                    writable=False,
                ),
                PurePosixPath("/srv/frigate/test-media"): PathStatus(
                    exists=True,
                    is_dir=False,
                    writable=False,
                ),
            }
        )

        results = run_doctor(
            deployment_config(),
            **baseline_probes(path_probe=paths),
        )
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["timezone.file"].level, "FAIL")
        self.assertEqual(by_code["runtime.path"].level, "FAIL")
        self.assertEqual(by_code["media.path"].level, "FAIL")
        self.assertEqual(by_code["media.space"].level, "FAIL")

    def test_grades_low_and_critically_low_media_space(self) -> None:
        probes = baseline_probes()
        paths = probes["path_probe"]
        self.assertIsInstance(paths, FakePathProbe)
        media_path = PurePosixPath("/srv/frigate/test-media")
        paths.statuses[media_path] = replace(
            paths.statuses[media_path],
            free_bytes=5 * 1024**3,
        )
        low_results = run_doctor(
            deployment_config(),
            **baseline_probes(path_probe=paths),
        )

        paths.statuses[media_path] = replace(
            paths.statuses[media_path],
            free_bytes=512 * 1024**2,
        )
        critical_results = run_doctor(
            deployment_config(),
            **baseline_probes(path_probe=paths),
        )

        self.assertEqual(
            {result.code: result for result in low_results}["media.space"].level,
            "WARN",
        )
        self.assertEqual(
            {result.code: result for result in critical_results}["media.space"].level,
            "FAIL",
        )

    def test_fails_occupied_and_duplicate_configured_ports(self) -> None:
        sockets = FakeSocketProbe(bindable={("127.0.0.1", 19080): False})
        occupied_results = run_doctor(
            deployment_config(),
            **baseline_probes(socket_probe=sockets),
        )
        occupied_by_code = {result.code: result for result in occupied_results}

        duplicate_config = replace(
            deployment_config(),
            admin=replace(deployment_config().admin, loopback_port=19080),
        )
        duplicate_sockets = FakeSocketProbe()
        duplicate_results = run_doctor(
            duplicate_config,
            **baseline_probes(socket_probe=duplicate_sockets),
        )
        duplicate_by_code = {result.code: result for result in duplicate_results}

        self.assertEqual(occupied_by_code["port.public"].level, "FAIL")
        self.assertEqual(duplicate_by_code["port.public"].level, "FAIL")
        self.assertEqual(duplicate_by_code["port.admin"].level, "FAIL")
        self.assertNotIn(("127.0.0.1", 19080), duplicate_sockets.bind_calls)

    def test_fails_when_an_enabled_camera_tcp_endpoint_is_unreachable(self) -> None:
        sockets = FakeSocketProbe(
            connectable={("192.168.1.128", 554): False},
        )

        results = run_doctor(
            deployment_config(),
            **baseline_probes(socket_probe=sockets),
        )
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["camera.tcp.yard"].level, "FAIL")

    def test_warns_when_a_protected_stream_cannot_be_probed_without_credentials(self) -> None:
        runner = FakeRunner(
            {
                ("docker", "info", "--format", "{{.ServerVersion}}"): CommandResult(0, "28.3.3"),
                ("docker", "compose", "version", "--short"): CommandResult(0, "2.39.1"),
                ("docker", "compose", "up", "--help"): CommandResult(
                    0, COMPOSE_UP_HELP_WITH_WAIT
                ),
                ("ffprobe", "-version"): CommandResult(0, "ffprobe 7.1"),
            }
        )

        results = run_doctor(
            deployment_config(),
            **baseline_probes(runner=runner),
        )
        by_code = {result.code: result for result in results}

        for code in ("camera.codec.ptz_view", "camera.codec.fixed_view"):
            self.assertEqual(by_code[code].level, "WARN")
            self.assertIn("verify after deployment", by_code[code].message)

    def test_cpu_mode_skips_render_device_inspection(self) -> None:
        paths = baseline_probes()["path_probe"]
        self.assertIsInstance(paths, FakePathProbe)

        results = run_doctor(
            deployment_config(),
            **baseline_probes(path_probe=paths),
        )
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["hardware.vaapi"].level, "PASS")
        self.assertFalse(any(str(path).startswith("/dev/dri/") for path in paths.inspected))

    def test_fails_an_inaccessible_or_non_character_intel_render_device(self) -> None:
        render_device = PurePosixPath("/dev/dri/renderD128")
        config = replace(
            deployment_config(),
            hardware=HardwareConfig(
                mode="intel-vaapi",
                render_device=render_device,
                va_driver="i965",
            ),
        )
        paths = FakePathProbe(
            {
                render_device: PathStatus(
                    exists=True,
                    is_char_device=False,
                    readable=True,
                    writable=True,
                )
            }
        )

        results = run_doctor(
            config,
            **baseline_probes(path_probe=paths),
        )
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["hardware.vaapi"].level, "FAIL")

    def test_does_not_read_password_files_or_put_secret_paths_in_results(self) -> None:
        runner = FakeRunner(
            {
                ("docker", "info", "--format", "{{.ServerVersion}}"): CommandResult(0, "28.3.3"),
                ("docker", "compose", "version", "--short"): CommandResult(0, "2.39.1"),
                ("docker", "compose", "up", "--help"): CommandResult(
                    0, COMPOSE_UP_HELP_WITH_WAIT
                ),
                ("ffprobe", "-version"): CommandResult(0, "ffprobe 7.1"),
            }
        )
        paths = baseline_probes()["path_probe"]
        self.assertIsInstance(paths, FakePathProbe)

        results = run_doctor(
            deployment_config(),
            **baseline_probes(runner=runner, path_probe=paths),
        )

        self.assertNotIn(Path("C:/sealed/yard.password"), paths.inspected)
        self.assertNotIn("yard.password", repr(runner.commands))
        self.assertNotIn("yard.password", repr(results))


if __name__ == "__main__":
    unittest.main()
