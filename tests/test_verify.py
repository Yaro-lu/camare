import json
import unittest
from dataclasses import replace
from pathlib import Path, PurePosixPath
from unittest import mock

from camarectl.checks import CommandResult
from camarectl.model import (
    AdminConfig,
    DeploymentConfig,
    GroupConfig,
    HardwareConfig,
    LanConfig,
    PublicConfig,
    StreamConfig,
)
from camarectl.verify import (
    HTTPResult,
    LocalHTTPProbe,
    RecordingSnapshot,
    verify_runtime,
)


def deployment_config(*, provider: str = "cloudflare") -> DeploymentConfig:
    return DeploymentConfig(
        instance_id="verify-test",
        runtime_dir=PurePosixPath("/opt/camare/verify-test"),
        timezone="Asia/Shanghai",
        provider=provider,
        public=PublicConfig(origin="https://camera.example.com", loopback_port=19080),
        admin=AdminConfig(
            origin="https://verify-test.example.ts.net",
            loopback_port=19081,
            tailscale_login="owner@example.com",
            frigate_bind_ip="127.0.0.1",
            frigate_port=18971,
        ),
        lan=LanConfig(bind_ip="192.168.1.250", port=18088),
        media=PurePosixPath("/srv/frigate/media"),
        retention=14,
        hardware=HardwareConfig(mode="cpu", render_device=None, va_driver=None),
        groups=(
            GroupConfig(
                id="yard",
                name="Yard",
                host="192.168.1.128",
                rtsp_user="admin",
                rtsp_port=554,
                password_file=Path("C:/sealed/camera.password"),
                streams=(
                    StreamConfig(id="ptz_view", name="PTZ", path="/live/101"),
                    StreamConfig(id="fixed_view", name="Fixed", path="/live/201"),
                ),
            ),
        ),
    )


class FakeRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.timeouts: list[float] = []
        self.ps = [
            {"Service": "frigate", "State": "running", "Health": ""},
            {"Service": "camera-portal", "State": "running", "Health": "healthy"},
            {"Service": "lan-caddy", "State": "running", "Health": "healthy"},
            {"Service": "cloudflared", "State": "running", "Health": ""},
        ]
        self.raw_ps: str | None = None
        self.stats: object = {
            "cameras": {
                "ptz_view": {
                    "pid": 101,
                    "capture_pid": 102,
                    "ffmpeg_pid": 103,
                    "camera_fps": 1,
                    "process_fps": 1,
                },
                "fixed_view": {
                    "pid": 201,
                    "capture_pid": 202,
                    "ffmpeg_pid": 203,
                    "camera_fps": 1,
                    "process_fps": 1,
                },
            }
        }
        self.streams: object = {
            "ptz_view": {"producers": [{}]},
            "fixed_view": {"producers": [{}]},
        }
        self.raw_stats: str | None = None
        self.raw_streams: str | None = None

    def run(self, command: tuple[str, ...], timeout: float) -> CommandResult:
        self.commands.append(command)
        self.timeouts.append(timeout)
        if "ps" in command:
            return CommandResult(
                0,
                self.raw_ps if self.raw_ps is not None else json.dumps(self.ps),
            )
        if command[-1].endswith("/api/stats"):
            return CommandResult(
                0,
                self.raw_stats if self.raw_stats is not None else json.dumps(self.stats),
            )
        if command[-1].endswith("/api/streams"):
            return CommandResult(
                0,
                self.raw_streams
                if self.raw_streams is not None
                else json.dumps(self.streams),
            )
        return CommandResult(127)


class FakeHTTP:
    def __init__(self) -> None:
        self.statuses: dict[str, int] = {}
        self.requests: list[str] = []
        self.timeouts: list[float] = []

    def get(self, url: str, timeout: float) -> HTTPResult:
        self.requests.append(url)
        self.timeouts.append(timeout)
        return HTTPResult(self.statuses.get(url, 200))


class FakePathProbe:
    def __init__(self) -> None:
        compose_digest = "a" * 64
        env_digest = "c" * 64
        frigate_digest = "d" * 64
        caddy_digest = "e" * 64
        self.manifest: object = {
            "format_version": 2,
            "instance_id": "verify-test",
            "provider": "cloudflare",
            "compose_files": ["compose.yaml"],
            "files": {
                ".env": env_digest,
                "compose.yaml": compose_digest,
                "caddy/Caddyfile": caddy_digest,
            },
            "mutable_files": ["frigate/config.yml"],
        }
        self.read_paths: list[Path] = []
        self.digest_paths: list[Path] = []
        self.file_digests: dict[Path, str | None] = {
            Path("C:/isolated/runtime/compose.yaml"): compose_digest,
            Path("C:/isolated/runtime/compose.intel.yaml"): "b" * 64,
            Path("C:/isolated/runtime/.env"): env_digest,
            Path("C:/isolated/runtime/frigate/config.yml"): frigate_digest,
            Path("C:/isolated/runtime/caddy/Caddyfile"): caddy_digest,
        }
        self.manifest_text: str | None = None
        self.recording_calls: list[str] = []
        self.recordings: dict[str, list[RecordingSnapshot | None]] = {
            "ptz_view": [
                RecordingSnapshot("old-ptz", 100, 1000),
                RecordingSnapshot("new-ptz", 200, 1000),
            ],
            "fixed_view": [
                RecordingSnapshot("fixed", 100, 1000),
                RecordingSnapshot("fixed", 110, 1200),
            ],
        }

    def read_text(self, path: Path, max_bytes: int) -> str:
        del max_bytes
        self.read_paths.append(path)
        return (
            self.manifest_text
            if self.manifest_text is not None
            else json.dumps(self.manifest)
        )

    def latest_recording(
        self,
        recordings_root: Path,
        stream_id: str,
    ) -> RecordingSnapshot | None:
        del recordings_root
        self.recording_calls.append(stream_id)
        sequence = self.recordings.get(stream_id, [None])
        index = min(self.recording_calls.count(stream_id) - 1, len(sequence) - 1)
        return sequence[index]

    def sha256_regular_file(self, path: Path, max_bytes: int) -> str | None:
        del max_bytes
        self.digest_paths.append(path)
        return self.file_digests.get(path)


class FakeSleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)


class VerifyRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path("C:/isolated/runtime")
        self.runner = FakeRunner()
        self.http = FakeHTTP()
        self.paths = FakePathProbe()
        self.sleeper = FakeSleeper()

    def verify(self, config: DeploymentConfig | None = None):
        return verify_runtime(
            config or deployment_config(),
            self.root,
            runner=self.runner,
            http=self.http,
            path=self.paths,
            sleeper=self.sleeper,
        )

    def test_healthy_cloudflare_runtime_passes_every_media_and_entrypoint_check(self) -> None:
        results = self.verify()
        by_code = {result.code: result for result in results}

        expected_passes = {
            "runtime.manifest",
            "compose.ps",
            "compose.service.frigate",
            "compose.service.camera-portal",
            "compose.service.lan-caddy",
            "compose.service.cloudflared",
            "frigate.stats.ptz_view",
            "frigate.stats.fixed_view",
            "go2rtc.producer.ptz_view",
            "go2rtc.producer.fixed_view",
            "recording.ptz_view",
            "recording.fixed_view",
            "http.portal.public",
            "http.portal.admin",
            "http.lan",
        }
        self.assertTrue(expected_passes.issubset(by_code))
        self.assertTrue(all(by_code[code].level == "PASS" for code in expected_passes))
        self.assertEqual(self.sleeper.calls, [12.0])
        self.assertEqual(self.paths.read_paths, [self.root / "manifest.json"])
        serialized = repr((self.runner.commands, results, self.paths.read_paths))
        self.assertNotIn("camera.password", serialized)
        self.assertTrue(all(0 < timeout <= 10 for timeout in self.runner.timeouts))
        self.assertTrue(all(0 < timeout <= 5 for timeout in self.http.timeouts))
        command_words = {word for command in self.runner.commands for word in command}
        self.assertTrue({"down", "prune"}.isdisjoint(command_words))
        self.assertIn("camare_verify-test", self.runner.commands[0])
        self.assertIn(str(self.root / "compose.yaml"), self.runner.commands[0])

    def test_missing_enabled_stream_fails_stats_and_producer_checks(self) -> None:
        self.runner.stats["cameras"].pop("fixed_view")
        self.runner.streams.pop("fixed_view")

        results = self.verify()
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["frigate.stats.fixed_view"].level, "FAIL")
        self.assertEqual(by_code["go2rtc.producer.fixed_view"].level, "FAIL")
        self.assertEqual(by_code["frigate.stats.ptz_view"].level, "PASS")
        self.assertEqual(by_code["go2rtc.producer.ptz_view"].level, "PASS")

    def test_unchanged_latest_mp4_fails_recording_progress(self) -> None:
        unchanged = RecordingSnapshot("fixed", 100, 1000)
        self.paths.recordings["fixed_view"] = [unchanged, unchanged]

        results = self.verify()
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["recording.fixed_view"].level, "FAIL")
        self.assertEqual(by_code["recording.ptz_view"].level, "PASS")
        self.assertEqual(self.sleeper.calls, [12.0])

    def test_each_failed_http_entrypoint_is_reported_independently(self) -> None:
        public_url = "http://127.0.0.1:19080/healthz"
        lan_url = "http://192.168.1.250:18088/healthz"
        self.http.statuses[public_url] = 503
        self.http.statuses[lan_url] = 0

        results = self.verify()
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["http.portal.public"].level, "FAIL")
        self.assertEqual(by_code["http.portal.admin"].level, "PASS")
        self.assertEqual(by_code["http.lan"].level, "FAIL")
        self.assertNotIn(public_url, repr(results))

    def test_malformed_manifest_fails_closed_before_any_runtime_probe(self) -> None:
        self.paths.manifest_text = "{not-json"

        results = self.verify()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].code, "runtime.manifest")
        self.assertEqual(results[0].level, "FAIL")
        self.assertEqual(self.runner.commands, [])
        self.assertEqual(self.http.requests, [])
        self.assertEqual(self.paths.recording_calls, [])
        self.assertEqual(self.sleeper.calls, [])

    def test_malformed_frigate_and_go2rtc_json_fail_closed(self) -> None:
        self.runner.raw_stats = "{not-json"
        self.runner.raw_streams = "[not-json"

        results = self.verify()
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["frigate.stats"].level, "FAIL")
        self.assertEqual(by_code["go2rtc.streams"].level, "FAIL")
        self.assertNotIn("frigate.stats.ptz_view", by_code)
        self.assertNotIn("go2rtc.producer.ptz_view", by_code)

    def test_tailscale_runtime_requires_only_the_three_local_services(self) -> None:
        config = deployment_config(provider="tailscale")
        self.paths.manifest["provider"] = "tailscale"
        self.runner.ps = [
            row for row in self.runner.ps if row["Service"] != "cloudflared"
        ]

        results = self.verify(config)
        by_code = {result.code: result for result in results}

        for service in ("frigate", "camera-portal", "lan-caddy"):
            self.assertEqual(by_code[f"compose.service.{service}"].level, "PASS")
        self.assertNotIn("compose.service.cloudflared", by_code)
        self.assertNotIn("compose.services.unexpected", by_code)

    def test_non_cloudflare_runtime_rejects_a_leftover_cloudflared_service(self) -> None:
        config = deployment_config(provider="lan")
        self.paths.manifest["provider"] = "lan"

        results = self.verify(config)
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["compose.services.unexpected"].level, "FAIL")

    def test_zero_pid_or_frame_rate_and_empty_producer_fail_closed(self) -> None:
        self.runner.stats["cameras"]["ptz_view"]["camera_fps"] = 0
        self.runner.streams["ptz_view"]["producers"] = []

        results = self.verify()
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["frigate.stats.ptz_view"].level, "FAIL")
        self.assertEqual(by_code["go2rtc.producer.ptz_view"].level, "FAIL")

    def test_unhealthy_or_missing_compose_service_fails_and_skips_exec(self) -> None:
        self.runner.ps = [
            row
            for row in self.runner.ps
            if row["Service"] not in {"frigate", "cloudflared"}
        ]
        self.runner.ps.append(
            {"Service": "frigate", "State": "running", "Health": "unhealthy"}
        )

        results = self.verify()
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["compose.service.frigate"].level, "FAIL")
        self.assertEqual(by_code["compose.service.cloudflared"].level, "FAIL")
        self.assertEqual(by_code["frigate.stats"].level, "FAIL")
        self.assertEqual(by_code["go2rtc.streams"].level, "FAIL")
        self.assertEqual(len(self.runner.commands), 1)
        self.assertEqual(self.sleeper.calls, [])
        self.assertEqual(self.paths.recording_calls, [])

    def test_malformed_compose_json_fails_without_querying_the_wrong_container(self) -> None:
        self.runner.raw_ps = "{not-json"

        results = self.verify()
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["compose.ps"].level, "FAIL")
        self.assertEqual(by_code["frigate.stats"].level, "FAIL")
        self.assertEqual(by_code["go2rtc.streams"].level, "FAIL")
        self.assertEqual(len(self.runner.commands), 1)
        self.assertEqual(self.sleeper.calls, [])

    def test_compose_ps_newline_delimited_json_is_supported(self) -> None:
        self.runner.raw_ps = "\n".join(json.dumps(row) for row in self.runner.ps)

        results = self.verify()
        by_code = {result.code: result for result in results}

        self.assertEqual(by_code["compose.ps"].level, "PASS")
        for service in ("frigate", "camera-portal", "lan-caddy", "cloudflared"):
            self.assertEqual(by_code[f"compose.service.{service}"].level, "PASS")

    def test_every_manifest_compose_file_is_used_for_project_queries_and_exec(self) -> None:
        config = replace(
            deployment_config(),
            hardware=HardwareConfig(
                mode="intel-vaapi",
                render_device=PurePosixPath("/dev/dri/renderD128"),
                va_driver="i965",
            ),
        )
        self.paths.manifest["compose_files"] = [
            "compose.yaml",
            "compose.intel.yaml",
        ]
        self.paths.manifest["files"]["compose.intel.yaml"] = "b" * 64

        results = self.verify(config)

        self.assertTrue(all(result.level == "PASS" for result in results))
        for command in self.runner.commands:
            self.assertIn(str(self.root / "compose.yaml"), command)
            self.assertIn(str(self.root / "compose.intel.yaml"), command)

    def test_missing_non_regular_or_hash_mismatched_compose_file_fails_closed(self) -> None:
        self.paths.file_digests[self.root / "compose.yaml"] = None

        missing_results = self.verify()

        self.assertEqual(missing_results[0].code, "runtime.manifest")
        self.assertEqual(missing_results[0].level, "FAIL")
        self.assertEqual(self.runner.commands, [])

        self.paths.file_digests[self.root / "compose.yaml"] = "b" * 64
        mismatched_results = self.verify()

        self.assertEqual(mismatched_results[0].code, "runtime.manifest")
        self.assertEqual(mismatched_results[0].level, "FAIL")
        self.assertEqual(self.runner.commands, [])

    def test_tampered_env_or_missing_mutable_frigate_config_fails_before_compose(self) -> None:
        self.paths.file_digests[self.root / ".env"] = "f" * 64

        env_results = self.verify()

        self.assertEqual(env_results[0].code, "runtime.manifest")
        self.assertEqual(env_results[0].level, "FAIL")
        self.assertEqual(self.runner.commands, [])

        self.paths.file_digests[self.root / ".env"] = "c" * 64
        self.paths.file_digests[self.root / "frigate/config.yml"] = None
        frigate_results = self.verify()

        self.assertEqual(frigate_results[0].code, "runtime.manifest")
        self.assertEqual(frigate_results[0].level, "FAIL")
        self.assertEqual(self.runner.commands, [])

    def test_changed_mutable_frigate_config_is_accepted(self) -> None:
        self.paths.file_digests[self.root / "frigate/config.yml"] = "f" * 64

        results = self.verify()

        self.assertFalse(
            any(result.code == "runtime.manifest" and result.level == "FAIL" for result in results)
        )
        self.assertTrue(self.runner.commands)

    def test_local_http_probe_explicitly_disables_environment_proxies(self) -> None:
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args: object) -> None:
                del args

        class FakeOpener:
            def open(self, request: object, timeout: float) -> FakeResponse:
                del request, timeout
                return FakeResponse()

        opener = FakeOpener()
        with (
            mock.patch(
                "camarectl.verify.urllib.request.build_opener",
                return_value=opener,
            ) as build_opener,
            mock.patch(
                "camarectl.verify.urllib.request.urlopen",
                return_value=FakeResponse(),
            ),
        ):
            response = LocalHTTPProbe().get("http://127.0.0.1:19080/healthz", 1)

        self.assertEqual(response.status_code, 200)
        build_opener.assert_called_once()
        proxy_handler = build_opener.call_args.args[0]
        self.assertEqual(proxy_handler.proxies, {})


if __name__ == "__main__":
    unittest.main()
