from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CLOUDFLARED_IMAGE = (
    "cloudflare/cloudflared:2026.7.3@"
    "sha256:e39ee8da81ad5e05d77f38d2f51c60ca51bf2a8450ac3abab50c17fdb91d91bf"
)
COMPOSE_FILES = (
    "frigate-1panel/docker-compose.yml",
    "camera-portal/compose.yaml",
    "camera-portal-lan/compose.yaml",
    "examples/cloudflare/compose.yaml",
)


class RepositoryPolicyTests(unittest.TestCase):
    def test_repository_is_published_under_mit_license(self) -> None:
        license_path = REPOSITORY_ROOT / "LICENSE"

        self.assertTrue(license_path.is_file(), "root LICENSE file is required")
        text = license_path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("MIT License\n\n"))
        self.assertIn("Copyright (c) 2026 Yaro-lu", text)
        self.assertIn("Permission is hereby granted, free of charge", text)
        self.assertIn('THE SOFTWARE IS PROVIDED "AS IS"', text)

    def test_changelog_tracks_unreleased_public_release_work(self) -> None:
        changelog_path = REPOSITORY_ROOT / "CHANGELOG.md"

        self.assertTrue(changelog_path.is_file(), "CHANGELOG.md is required")
        text = changelog_path.read_text(encoding="utf-8")
        self.assertIn("## [Unreleased]", text)
        self.assertIn("### Changed", text)
        self.assertIn("默认不录制声音", text)

    def test_frigate_template_records_without_audio_by_default(self) -> None:
        text = (
            REPOSITORY_ROOT / "frigate-1panel" / "config" / "config.yml"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "  output_args:\n    record: preset-record-generic\n",
            text,
        )
        self.assertNotIn("preset-record-generic-audio", text)

    def test_cloudflared_template_uses_an_immutable_release(self) -> None:
        compose = (
            REPOSITORY_ROOT / "examples" / "cloudflare" / "compose.yaml"
        ).read_text(encoding="utf-8")
        example_env = (
            REPOSITORY_ROOT / "examples" / "cloudflare" / ".env.example"
        ).read_text(encoding="utf-8")

        self.assertIn(CLOUDFLARED_IMAGE, compose)
        self.assertIn(f"CLOUDFLARED_IMAGE={CLOUDFLARED_IMAGE}", example_env)
        self.assertNotIn("cloudflare/cloudflared:latest", compose)
        self.assertNotIn("cloudflare/cloudflared:latest", example_env)

        installer = (REPOSITORY_ROOT / "deploy.py").read_text(encoding="utf-8")
        tag, digest = CLOUDFLARED_IMAGE.split("@", 1)
        self.assertIn(tag + "@", installer)
        self.assertIn(digest, installer)
        self.assertNotIn('"cloudflare/cloudflared:latest"', installer)

    def test_single_stack_uses_host_metadata_for_local_file_secrets(self) -> None:
        compose = (REPOSITORY_ROOT / "deploy" / "compose.base.yaml").read_text(
            encoding="utf-8"
        )
        caddy_service = compose.split("  lan-caddy:", 1)[1].split(
            "@@CLOUDFLARED_SERVICE@@", 1
        )[0]
        top_level_secrets = compose.split("\nsecrets:\n", 1)[1].split(
            "\nnetworks:\n", 1
        )[0]

        self.assertEqual(compose.count("group_add:"), 1)
        self.assertIn('    group_add:\n      - "65532"\n', caddy_service)
        # Compose cannot implement uid/gid/mode for a local file source; the
        # rendered manifest applies those values to the host files instead.
        self.assertNotIn("uid:", top_level_secrets)
        self.assertNotIn("gid:", top_level_secrets)
        self.assertNotIn("mode:", top_level_secrets)

    def test_caddy_healthchecks_use_busybox_wget_without_curl(self) -> None:
        compose_paths = (
            "deploy/compose.base.yaml",
            "camera-portal-lan/compose.yaml",
        )

        for relative_path in compose_paths:
            with self.subTest(compose=relative_path):
                compose = (REPOSITORY_ROOT / relative_path).read_text(
                    encoding="utf-8"
                )
                caddy_service = compose.split("  lan-caddy:", 1)[1]
                if relative_path == "deploy/compose.base.yaml":
                    caddy_service = caddy_service.split(
                        "@@CLOUDFLARED_SERVICE@@", 1
                    )[0]
                else:
                    caddy_service = caddy_service.split("\nnetworks:\n", 1)[0]

                self.assertIn("healthcheck:", caddy_service)
                self.assertIn("wget", caddy_service)
                self.assertIn("--spider", caddy_service)
                self.assertIn(
                    "http://127.0.0.1:8080/healthz",
                    caddy_service,
                )
                self.assertNotIn("curl", caddy_service)

    def test_compose_projects_do_not_claim_global_container_names(self) -> None:
        for relative_path in COMPOSE_FILES:
            with self.subTest(compose=relative_path):
                text = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertNotIn("container_name:", text)

    def test_legacy_verifiers_discover_compose_services_instead_of_fixed_names(self) -> None:
        frigate = (REPOSITORY_ROOT / "frigate-1panel" / "verify.sh").read_text(
            encoding="utf-8"
        )
        lan = (
            REPOSITORY_ROOT / "camera-portal-lan" / "scripts" / "verify.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("docker compose ps -q frigate", frigate)
        self.assertIn('"$frigate_id"', frigate)
        self.assertNotIn("docker exec frigate", frigate)
        self.assertIn("docker compose ps -q lan-caddy", lan)
        self.assertIn("com.docker.compose.service", lan)
        self.assertNotIn("docker inspect camera-portal \\", lan)
        self.assertNotIn("docker inspect camera-portal-lan-caddy", lan)

    def test_local_builds_use_normal_networking_unless_explicitly_overridden(self) -> None:
        expected = {
            "camera-portal/compose.yaml": (
                'network: "${PORTAL_BUILD_NETWORK:-default}"',
                "PORTAL_BUILD_NETWORK=default",
                "camera-portal/.env.example",
            ),
            "camera-portal-lan/compose.yaml": (
                'network: "${LAN_CADDY_BUILD_NETWORK:-default}"',
                "LAN_CADDY_BUILD_NETWORK=default",
                "camera-portal-lan/.env.example",
            ),
        }
        for compose_path, (network_line, env_line, env_path) in expected.items():
            with self.subTest(compose=compose_path):
                compose = (REPOSITORY_ROOT / compose_path).read_text(encoding="utf-8")
                example_env = (REPOSITORY_ROOT / env_path).read_text(encoding="utf-8")
                self.assertIn(network_line, compose)
                self.assertNotIn("network: host", compose)
                self.assertIn(env_line, example_env)

    def test_compose_runtime_identity_is_configurable_with_compatible_defaults(self) -> None:
        expected = {
            "frigate-1panel/docker-compose.yml": {
                "env": "frigate-1panel/.env.example",
                "compose_lines": (
                    'name: "${FRIGATE_PROJECT_NAME:-frigate}"',
                    'image: "${FRIGATE_IMAGE:-ghcr.io/blakeblackshear/frigate:0.17.2@sha256:d4351369984d4a9e2a49ac59736f6490856a7ea11f7790040746d21496967010}"',
                    'name: "${FRIGATE_DOCKER_NETWORK:-frigate_default}"',
                    '"${FRIGATE_ADMIN_BIND_IP:-127.0.0.1}:${FRIGATE_ADMIN_PORT:-8971}:8971/tcp"',
                ),
                "env_lines": (
                    "FRIGATE_PROJECT_NAME=frigate",
                    "FRIGATE_IMAGE=ghcr.io/blakeblackshear/frigate:0.17.2@sha256:d4351369984d4a9e2a49ac59736f6490856a7ea11f7790040746d21496967010",
                    "FRIGATE_DOCKER_NETWORK=frigate_default",
                    "FRIGATE_ADMIN_BIND_IP=127.0.0.1",
                    "FRIGATE_ADMIN_PORT=8971",
                ),
            },
            "camera-portal/compose.yaml": {
                "env": "camera-portal/.env.example",
                "compose_lines": (
                    'name: "${PORTAL_PROJECT_NAME:-camera-portal}"',
                    'image: "${CAMERA_PORTAL_IMAGE:-local/camera-portal:1.0.0}"',
                    'name: "${FRIGATE_DOCKER_NETWORK:-frigate_default}"',
                    'name: "${PORTAL_INGRESS_NETWORK:-camera_portal_tunnel}"',
                    '"127.0.0.1:${PUBLIC_LOOPBACK_PORT:-9080}:8080"',
                    '"127.0.0.1:${ADMIN_LOOPBACK_PORT:-9081}:8081"',
                ),
                "env_lines": (
                    "PORTAL_PROJECT_NAME=camera-portal",
                    "CAMERA_PORTAL_IMAGE=local/camera-portal:1.0.0",
                    "FRIGATE_DOCKER_NETWORK=frigate_default",
                    "PORTAL_INGRESS_NETWORK=camera_portal_tunnel",
                    "PUBLIC_LOOPBACK_PORT=9080",
                    "ADMIN_LOOPBACK_PORT=9081",
                ),
            },
            "camera-portal-lan/compose.yaml": {
                "env": "camera-portal-lan/.env.example",
                "compose_lines": (
                    'name: "${LAN_PROJECT_NAME:-camera-portal-lan}"',
                    'image: "${LAN_CADDY_IMAGE:-local/camera-portal-lan-caddy:2.11.4}"',
                    'name: "${PORTAL_INGRESS_NETWORK:-camera_portal_tunnel}"',
                    '"${LAN_BIND_IP:?set LAN_BIND_IP in .env}:${LAN_PORT:-8088}:8080/tcp"',
                ),
                "env_lines": (
                    "LAN_PROJECT_NAME=camera-portal-lan",
                    "LAN_CADDY_IMAGE=local/camera-portal-lan-caddy:2.11.4",
                    "PORTAL_INGRESS_NETWORK=camera_portal_tunnel",
                    "LAN_BIND_IP=192.168.1.250",
                    "LAN_PORT=8088",
                ),
            },
            "examples/cloudflare/compose.yaml": {
                "env": "examples/cloudflare/.env.example",
                "compose_lines": (
                    'name: "${CLOUDFLARE_PROJECT_NAME:-camera-portal-cloudflare}"',
                    f'image: "${{CLOUDFLARED_IMAGE:-{CLOUDFLARED_IMAGE}}}"',
                    'name: "${PORTAL_INGRESS_NETWORK:-camera_portal_tunnel}"',
                ),
                "env_lines": (
                    "CLOUDFLARE_PROJECT_NAME=camera-portal-cloudflare",
                    f"CLOUDFLARED_IMAGE={CLOUDFLARED_IMAGE}",
                    "PORTAL_INGRESS_NETWORK=camera_portal_tunnel",
                ),
            },
        }

        for compose_path, policy in expected.items():
            with self.subTest(compose=compose_path):
                compose = (REPOSITORY_ROOT / compose_path).read_text(encoding="utf-8")
                example_env = (REPOSITORY_ROOT / policy["env"]).read_text(
                    encoding="utf-8"
                )
                for line in policy["compose_lines"]:
                    self.assertIn(line, compose)
                for line in policy["env_lines"]:
                    self.assertIn(line, example_env)


if __name__ == "__main__":
    unittest.main()
