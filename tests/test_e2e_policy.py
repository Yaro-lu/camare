from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class E2EPolicyTests(unittest.TestCase):
    def test_synthetic_sources_are_bridge_only_and_support_two_or_four_streams(self) -> None:
        compose_path = REPOSITORY_ROOT / "tests" / "e2e" / "compose.yaml"

        self.assertTrue(compose_path.is_file(), "tests/e2e/compose.yaml is required")
        compose = compose_path.read_text(encoding="utf-8")
        self.assertIn("source-primary:", compose)
        self.assertIn("source-secondary:", compose)
        self.assertIn('profiles: ["four-streams"]', compose)
        self.assertIn("${E2E_BIND_IP:?", compose)
        self.assertIn("${SOURCE_PRIMARY_PORT:?", compose)
        self.assertIn("${SOURCE_SECONDARY_PORT:?", compose)
        for path in ("CAM1", "CAM2", "CAM3", "CAM4"):
            self.assertIn(f"MTX_PATHS_{path}_RUNONINIT", compose)
        for forbidden in (
            "network_mode: host",
            "container_name:",
            "0.0.0.0",
            "192.168.",
            "latest",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, compose)

    def test_server_audit_reads_container_state_without_mutating_docker(self) -> None:
        audit_path = REPOSITORY_ROOT / "scripts" / "server-staging-audit.sh"

        self.assertTrue(audit_path.is_file(), "server staging audit is required")
        audit = audit_path.read_text(encoding="utf-8")
        self.assertTrue(audit.startswith("#!/usr/bin/env bash\nset -Eeuo pipefail\n"))
        self.assertIn("server-staging-audit.sh capture --output FILE", audit)
        self.assertIn("server-staging-audit.sh compare --baseline FILE", audit)
        self.assertIn('action == "capture"', audit)
        self.assertIn('docker", "inspect"', audit)
        self.assertIn('"Config.Image"', audit)
        self.assertIn('"State.StartedAt"', audit)
        self.assertIn('"RestartCount"', audit)
        self.assertIn('"NetworkSettings.Ports"', audit)
        self.assertIn('"NetworkSettings.Networks"', audit)
        self.assertIn('"Mounts"', audit)
        self.assertNotIn('"Config.Env"', audit)
        for forbidden in (
            "docker rm",
            "docker stop",
            "docker restart",
            "docker compose",
            "docker network rm",
            "docker image rm",
            "prune",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, audit)

    def test_runner_uses_unique_e2e_identity_bridge_binding_and_exact_cleanup(self) -> None:
        runner_path = REPOSITORY_ROOT / "tests" / "e2e" / "run.sh"

        self.assertTrue(runner_path.is_file(), "tests/e2e/run.sh is required")
        runner = runner_path.read_text(encoding="utf-8")
        self.assertTrue(runner.startswith("#!/usr/bin/env bash\nset -Eeuo pipefail\n"))
        self.assertIn("umask 077", runner)
        self.assertIn("--run-id", runner)
        self.assertIn("--streams 2|4", runner)
        self.assertIn('^e2e[a-z0-9_-]{0,28}$', runner)
        self.assertIn("docker network inspect bridge", runner)
        self.assertIn("com.yaro-lu.camare.e2e.run-id", runner)
        self.assertIn('label=com.docker.compose.project=$project', runner)
        self.assertIn('remove_exact_project_objects "$CAMARE_PROJECT"', runner)
        self.assertIn('remove_exact_project_objects "$SOURCE_PROJECT"', runner)
        self.assertIn('run-local image already exists', runner)
        self.assertIn('trap \'on_exit $?\' EXIT', runner)
        self.assertIn('"$resolved_run_root" == "$STAGING_BASE/$RUN_ID"', runner)
        self.assertIn('rm -rf -- "$resolved_run_root"', runner)
        for forbidden in (
            "network_mode: host",
            "docker system prune",
            "docker image prune",
            "docker network prune",
            "docker volume prune",
            "down -v",
            "down --volumes",
            "docker compose config",
            "set -x",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, runner)

    def test_runner_requires_a_precreated_private_root_staging_directory(self) -> None:
        runner = (REPOSITORY_ROOT / "tests" / "e2e" / "run.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("staging base must already exist", runner)
        self.assertIn("staging base must be a directory", runner)
        self.assertIn("staging base cannot be a symlink", runner)
        self.assertIn("staging base must be root:root mode 0700", runner)
        self.assertIn("realpath -e", runner)
        self.assertIn("stat -c '%u:%g:%a'", runner)
        self.assertIn('"0:0:700"', runner)

    def test_runner_rejects_household_lan_bridge_ranges(self) -> None:
        runner = (REPOSITORY_ROOT / "tests" / "e2e" / "run.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn('"10.0.0.0/8"', runner)
        self.assertIn('"172.16.0.0/12"', runner)
        self.assertNotIn('"192.168.0.0/16"', runner)
        self.assertIn("Docker bridge gateway must use 10/8 or 172.16/12", runner)
        self.assertIn("192.168/16 is forbidden", runner)

    def test_runner_serializes_heavy_e2e_without_systemd(self) -> None:
        runner = (REPOSITORY_ROOT / "tests" / "e2e" / "run.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("E2E_LOCK_PATH=/run/camare-e2e.lock", runner)
        self.assertIn('exec {E2E_LOCK_FD}<>"$E2E_LOCK_PATH"', runner)
        self.assertIn('flock -n "$E2E_LOCK_FD"', runner)
        self.assertIn("another Camare E2E run is active", runner)
        self.assertNotIn("systemd-run", runner)

    def test_runner_keeps_synthetic_secrets_private_and_heavy_e2e_manual(self) -> None:
        runner = (REPOSITORY_ROOT / "tests" / "e2e" / "run.sh").read_text(
            encoding="utf-8"
        )
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "checks.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("secrets.token_urlsafe(32)", runner)
        self.assertGreaterEqual(runner.count("install -m 0600"), 2)
        self.assertIn('chmod 0600 "$CAMARE_CONFIG"', runner)
        self.assertIn("--audit-recording-container", runner)
        self.assertIn('bash "$AUDIT_SCRIPT" compare', runner)
        self.assertNotIn('cat "$INPUT_ROOT/secrets', runner)
        self.assertNotIn("Config.Env", runner)
        self.assertNotIn("tests/e2e/run.sh", workflow)
        self.assertNotIn("server-staging-audit.sh", workflow)

    def test_runner_checks_runtime_secret_modes_and_real_lan_replay_path(self) -> None:
        runner = (REPOSITORY_ROOT / "tests" / "e2e" / "run.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("0:65532:440", runner)
        self.assertIn("0:0:600", runner)
        self.assertIn('"$RUNTIME_ROOT/secrets/lan-access-key"', runner)
        self.assertIn('"$RUNTIME_ROOT/secrets/camera-password-1"', runner)
        self.assertIn("urllib.request.ProxyHandler({})", runner)
        self.assertIn('"/api/config"', runner)
        self.assertIn('"/api/recordings?"', runner)
        self.assertIn('"/media/vod/"', runner)
        self.assertIn('"/start/"', runner)
        self.assertIn('"/master.m3u8"', runner)
        self.assertIn('body.startswith(b"#EXTM3U")', runner)
        self.assertIn("for attempt in range(20)", runner)


if __name__ == "__main__":
    unittest.main()
