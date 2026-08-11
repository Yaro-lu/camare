import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from camarectl.checks import CheckResult
from camarectl.layout import create_staging_layout
from camarectl.model import (
    AdminConfig,
    DeploymentConfig,
    GroupConfig,
    HardwareConfig,
    LanConfig,
    PublicConfig,
    StreamConfig,
)
from camarectl.runtime import ApplyResult, StagingValidation
from camarectl.verify import VerificationResult


def deployment_config(runtime_dir: PurePosixPath) -> DeploymentConfig:
    return DeploymentConfig(
        instance_id="cli-test",
        runtime_dir=runtime_dir,
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
        media=PurePosixPath("/srv/camare/media"),
        retention=14,
        hardware=HardwareConfig(mode="cpu", render_device=None, va_driver=None),
        groups=(
            GroupConfig(
                id="yard",
                name="Yard",
                host="192.168.1.128",
                rtsp_user="private-user",
                rtsp_port=554,
                password_file=Path("C:/do-not-print/camera.password"),
                streams=(
                    StreamConfig(id="yard_ptz", name="PTZ", path="/live/101"),
                    StreamConfig(id="yard_fixed", name="Fixed", path="/live/201"),
                ),
            ),
        ),
    )


def capture_main(arguments: list[str]) -> tuple[int, str]:
    from camarectl.commands import main

    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        code = main(arguments)
    return code, output.getvalue()


class CLITests(unittest.TestCase):
    def test_init_copies_the_example_without_overwriting_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "configuration" / "camare.ini"

            code, output = capture_main(["init", "--config", str(destination)])

            self.assertEqual(code, 0)
            self.assertTrue(destination.is_file())
            self.assertTrue((destination.parent / "secrets").is_dir())
            original = destination.read_bytes()
            self.assertIn("camarectl validate", output)

            second_code, second_output = capture_main(
                ["init", "--config", str(destination)]
            )
            self.assertEqual(second_code, 2)
            self.assertEqual(destination.read_bytes(), original)
            self.assertIn("already exists", second_output)

    def test_validate_prints_only_non_secret_summary(self) -> None:
        config = deployment_config(PurePosixPath("/opt/camare/cli-test"))
        with mock.patch("camarectl.commands.load_config", return_value=config):
            code, output = capture_main(
                ["validate", "--config", "ignored.ini", "--json"]
            )

        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["instance_id"], "cli-test")
        self.assertEqual(payload["groups"], 1)
        self.assertEqual(payload["streams"], 2)
        self.assertNotIn("camera.password", output)
        self.assertNotIn("private-user", output)
        self.assertNotIn("192.168.1.128", output)

    def test_doctor_returns_failure_when_any_required_check_fails(self) -> None:
        config = deployment_config(PurePosixPath("/opt/camare/cli-test"))
        results = (
            CheckResult("PASS", "docker.engine", "Docker is ready"),
            CheckResult("FAIL", "camera.tcp.yard", "Camera is unreachable"),
        )
        with (
            mock.patch("camarectl.commands.load_config", return_value=config),
            mock.patch("camarectl.commands.run_doctor", return_value=results),
        ):
            code, output = capture_main(
                ["doctor", "--config", "ignored.ini", "--json"]
            )

        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertEqual([item["level"] for item in payload], ["PASS", "FAIL"])
        self.assertNotIn("camera.password", output)

    def test_plan_renders_validates_and_removes_temporary_controls(self) -> None:
        config = deployment_config(PurePosixPath("/opt/camare/cli-test"))
        rendered_roots: list[Path] = []

        def render(config_value, *, source_root, staging_parent, lan_access_key=None):
            del config_value, source_root, lan_access_key
            layout = create_staging_layout(
                staging_parent,
                "cli-test",
                include_intel=False,
            )
            rendered_roots.append(layout.root)
            return layout

        validation = StagingValidation(
            root=Path("C:/temporary/staging"),
            project_name="camare_cli-test",
            compose_files=(Path("C:/temporary/staging/compose.yaml"),),
        )
        with (
            mock.patch("camarectl.commands.load_config", return_value=config),
            mock.patch("camarectl.commands.render_runtime", side_effect=render),
            mock.patch(
                "camarectl.commands.validate_staging", return_value=validation
            ) as validate,
        ):
            code, output = capture_main(
                ["plan", "--config", "ignored.ini", "--json"]
            )

        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["project_name"], "camare_cli-test")
        self.assertEqual(payload["services"], 3)
        self.assertEqual(payload["streams"], 2)
        validate.assert_called_once()
        self.assertTrue(rendered_roots)
        self.assertTrue(all(not root.exists() for root in rendered_roots))
        self.assertNotIn("camera.password", output)

    def test_apply_stops_before_render_when_preflight_has_a_failure(self) -> None:
        config = deployment_config(PurePosixPath("/opt/camare/cli-test"))
        with (
            mock.patch("camarectl.commands.load_config", return_value=config),
            mock.patch(
                "camarectl.commands.run_doctor",
                return_value=(CheckResult("FAIL", "docker.engine", "unavailable"),),
            ),
            mock.patch("camarectl.commands.render_runtime") as render,
        ):
            code, output = capture_main(
                ["apply", "--config", "ignored.ini", "--yes"]
            )

        self.assertEqual(code, 1)
        self.assertIn("preflight", output.lower())
        render.assert_not_called()

    def test_apply_uses_transactional_runtime_and_media_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "camare-cli-test"
            config = deployment_config(PurePosixPath(target.as_posix()))
            layout_holder: list[object] = []

            def render(config_value, *, source_root, staging_parent, lan_access_key=None):
                del config_value, source_root, lan_access_key
                layout = create_staging_layout(
                    staging_parent,
                    "cli-test",
                    include_intel=False,
                )
                layout_holder.append(layout)
                return layout

            verification = (
                VerificationResult("PASS", "recording.yard_ptz", "advanced"),
            )

            def apply(layout, target_root, runner, verifier=None, **kwargs):
                del runner, kwargs
                self.assertEqual(layout, layout_holder[0])
                self.assertEqual(target_root, target)
                self.assertIsNotNone(verifier)
                self.assertTrue(verifier(target))
                return ApplyResult(
                    target_root=target,
                    project_name="camare_cli-test",
                    installed=True,
                    changed=True,
                    verified=True,
                )

            with (
                mock.patch("camarectl.commands.load_config", return_value=config),
                mock.patch(
                    "camarectl.commands.run_doctor",
                    return_value=(CheckResult("PASS", "host", "ready"),),
                ),
                mock.patch("camarectl.commands.render_runtime", side_effect=render),
                mock.patch("camarectl.commands.apply_staging", side_effect=apply),
                mock.patch(
                    "camarectl.commands.verify_runtime", return_value=verification
                ) as verify,
            ):
                code, output = capture_main(
                    ["apply", "--config", "ignored.ini", "--yes", "--json"]
                )

            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertTrue(payload["installed"])
            self.assertTrue(payload["verified"])
            verify.assert_called_once_with(config, target)
            self.assertNotIn("camera.password", output)

    def test_apply_requires_explicit_confirmation_in_noninteractive_mode(self) -> None:
        config = deployment_config(PurePosixPath("/opt/camare/cli-test"))
        with (
            mock.patch("camarectl.commands.load_config", return_value=config),
            mock.patch(
                "camarectl.commands.run_doctor",
                return_value=(CheckResult("PASS", "host", "ready"),),
            ),
            mock.patch("camarectl.commands.sys.stdin.isatty", return_value=False),
            mock.patch("camarectl.commands.render_runtime") as render,
        ):
            code, output = capture_main(["apply", "--config", "ignored.ini"])

        self.assertEqual(code, 2)
        self.assertIn("--yes", output)
        render.assert_not_called()

    def test_verify_and_status_are_read_only_and_fail_on_broken_media(self) -> None:
        config = deployment_config(PurePosixPath("/opt/camare/cli-test"))
        results = (
            VerificationResult("PASS", "compose.ps", "readable"),
            VerificationResult("FAIL", "recording.yard_ptz", "not advancing"),
        )
        with (
            mock.patch("camarectl.commands.load_config", return_value=config),
            mock.patch("camarectl.commands.verify_runtime", return_value=results) as verify,
        ):
            verify_code, verify_output = capture_main(
                ["verify", "--config", "ignored.ini", "--json"]
            )
            status_code, status_output = capture_main(
                ["status", "--config", "ignored.ini", "--json"]
            )

        self.assertEqual(verify_code, 1)
        self.assertEqual(status_code, 1)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(json.loads(verify_output)[1]["level"], "FAIL")
        self.assertEqual(json.loads(status_output)["level"], "FAIL")
        self.assertNotIn("camera.password", verify_output + status_output)

    def test_install_script_uses_new_cli_and_keeps_legacy_explicit(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "install.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn('python3 -m camarectl', script)
        self.assertIn('legacy', script)
        self.assertIn('deploy.py', script)
        self.assertTrue(script.rstrip().endswith('exec python3 -m camarectl "$@"'))


if __name__ == "__main__":
    unittest.main()
