import json
from pathlib import Path
import re
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class SupplyChainPolicyTests(unittest.TestCase):
    def test_portal_web_dependencies_are_locked_and_installed_without_scripts(self) -> None:
        portal = REPOSITORY_ROOT / "camera-portal"
        package_path = portal / "package.json"
        lock_path = portal / "package-lock.json"

        self.assertTrue(package_path.is_file(), "camera-portal/package.json is required")
        self.assertTrue(lock_path.is_file(), "camera-portal/package-lock.json is required")

        package = json.loads(package_path.read_text(encoding="utf-8"))
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        expected_dependencies = {
            "@cycjimmy/jsmpeg-player": "6.1.2",
            "hls.js": "1.6.16",
        }

        self.assertTrue(package["private"])
        self.assertEqual(package["dependencies"], expected_dependencies)
        self.assertEqual(
            package["scripts"]["test"],
            "node --test web/tests/*.test.mjs",
        )
        self.assertEqual(lock["lockfileVersion"], 3)
        self.assertEqual(lock["packages"][""]["dependencies"], expected_dependencies)

        dockerfile = (portal / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY package.json package-lock.json ./", dockerfile)
        self.assertIn("npm ci --ignore-scripts --no-audit --no-fund", dockerfile)
        self.assertNotIn("npm install", dockerfile)

    def test_container_build_inputs_are_immutable_and_traceable(self) -> None:
        portal_dockerfile = (
            REPOSITORY_ROOT / "camera-portal" / "Dockerfile"
        ).read_text(encoding="utf-8")
        caddy_dockerfile = (
            REPOSITORY_ROOT / "camera-portal-lan" / "caddy" / "Dockerfile"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "FROM node:22.19.0-alpine3.22@sha256:"
            "d2166de198f26e17e5a442f537754dd616ab069c47cc57b889310a717e0abbf9 "
            "AS webdeps",
            portal_dockerfile,
        )
        self.assertIn(
            "FROM golang:1.26.5-alpine3.24@sha256:"
            "0178a641fbb4858c5f1b48e34bdaabe0350a330a1b1149aabd498d0699ff5fb2 "
            "AS build",
            portal_dockerfile,
        )
        self.assertIn(
            "FROM caddy:2.11.4-alpine@sha256:"
            "5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648",
            caddy_dockerfile,
        )
        self.assertNotIn("ARG CADDY_VERSION", caddy_dockerfile)
        self.assertIn("go mod verify", portal_dockerfile)

        source_label = (
            'LABEL org.opencontainers.image.source="https://github.com/Yaro-lu/camare"'
        )
        self.assertIn(source_label, portal_dockerfile)
        self.assertIn(source_label, caddy_dockerfile)

    def test_caddy_image_rebuilds_fixed_sources_without_vulnerable_dependencies(
        self,
    ) -> None:
        dockerfile = (
            REPOSITORY_ROOT / "camera-portal-lan" / "caddy" / "Dockerfile"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "FROM golang:1.26.5-alpine3.24@sha256:"
            "0178a641fbb4858c5f1b48e34bdaabe0350a330a1b1149aabd498d0699ff5fb2 "
            "AS build",
            dockerfile,
        )
        self.assertIn("GOTOOLCHAIN=local", dockerfile)
        self.assertIn(
            "go get github.com/caddyserver/caddy/v2@v2.11.4",
            dockerfile,
        )
        self.assertIn("golang.org/x/text@v0.39.0", dockerfile)
        self.assertIn("google.golang.org/grpc@v1.82.1", dockerfile)
        self.assertIn("go mod verify", dockerfile)
        self.assertIn("CGO_ENABLED=0 go build", dockerfile)
        self.assertIn("-mod=readonly", dockerfile)
        self.assertIn("apk del --no-network curl", dockerfile)
        self.assertIn(
            "COPY --from=build /out/caddy /usr/bin/caddy",
            dockerfile,
        )
        self.assertNotIn("apk upgrade", dockerfile)

    def test_pr_ci_has_immutable_least_privilege_execution(self) -> None:
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "checks.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("    branches: [main]", workflow)
        self.assertIn("  pull_request:\n", workflow)
        self.assertIn("  workflow_call:\n", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertNotIn("self-hosted", workflow)
        self.assertNotIn("ubuntu-latest", workflow)
        self.assertNotIn("${{ secrets.", workflow)
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("concurrency:", workflow)
        self.assertIn("cancel-in-progress: true", workflow)

        uses_lines = re.findall(
            r"(?m)^\s*- uses: ([^\s]+)(?:\s+#\s*(.+))?$",
            workflow,
        )
        self.assertTrue(uses_lines, "workflow must use pinned setup actions")
        for action, version_comment in uses_lines:
            with self.subTest(action=action):
                self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")
                self.assertRegex(version_comment, r"^v\d")

        checkout_count = sum(
            action.startswith("actions/checkout@") for action, _ in uses_lines
        )
        self.assertGreater(checkout_count, 0)
        self.assertEqual(
            workflow.count("persist-credentials: false"),
            checkout_count,
        )
        self.assertEqual(
            workflow.count("runs-on: ubuntu-24.04"),
            workflow.count("timeout-minutes:"),
        )

    def test_ci_runs_python_tests_on_the_supported_version_floor(self) -> None:
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "checks.yml"
        ).read_text(encoding="utf-8")

        self.assertIn('python-version: ["3.10", "3.13"]', workflow)
        self.assertIn(
            "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 "
            "# v7.0.0",
            workflow,
        )
        self.assertIn("python -m py_compile deploy.py", workflow)
        self.assertIn(
            "python -m unittest discover -s tests -p 'test_*.py'",
            workflow,
        )

    def test_ci_formats_vets_and_tests_the_go_portal(self) -> None:
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "checks.yml"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "actions/setup-go@b7ad1dad31e06c5925ef5d2fc7ad053ef454303e "
            "# v7.0.0",
            workflow,
        )
        for command in (
            "gofmt -l .",
            "go mod verify",
            "go vet ./...",
            "go test ./...",
            "go test -race ./internal/portal",
        ):
            with self.subTest(command=command):
                self.assertIn(command, workflow)

    def test_ci_installs_and_runs_all_node_tests(self) -> None:
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "checks.yml"
        ).read_text(encoding="utf-8")
        expected_tests = {
            "camera-groups.test.mjs",
            "replay-audio.test.mjs",
            "timeline-core.test.mjs",
        }
        actual_tests = {
            path.name
            for path in (REPOSITORY_ROOT / "camera-portal" / "web" / "tests").glob(
                "*.test.mjs"
            )
        }

        self.assertTrue(expected_tests.issubset(actual_tests))
        self.assertIn(
            "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 "
            "# v7.0.0",
            workflow,
        )
        self.assertIn("node-version: 22.19.0", workflow)
        self.assertIn("npm ci --ignore-scripts --no-audit --no-fund", workflow)
        self.assertIn("npm test", workflow)

    def test_ci_lints_renders_and_builds_deployment_assets(self) -> None:
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "checks.yml"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "koalaman/shellcheck-alpine:v0.11.0@sha256:"
            "9955be09ea7f0dbf7ae942ac1f2094355bb30d96fffba0ec09f5432207544002",
            workflow,
        )
        self.assertIn("bash -n install.sh", workflow)
        self.assertIn(
            '"$SHELLCHECK_IMAGE" shellcheck --exclude=SC1007 \\', workflow
        )

        for project in (
            "frigate-1panel",
            "camera-portal",
            "camera-portal-lan",
            "examples/cloudflare",
        ):
            with self.subTest(project=project):
                self.assertIn(
                    f"docker compose --project-directory {project} config --quiet",
                    workflow,
                )

        self.assertIn(
            "docker build --pull --tag local/camera-portal:ci camera-portal",
            workflow,
        )
        self.assertIn(
            "docker build --pull --tag local/camera-portal-lan-caddy:ci "
            "camera-portal-lan/caddy",
            workflow,
        )

    def test_ci_blocks_leaked_secrets_and_high_risk_dependencies(self) -> None:
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "checks.yml"
        ).read_text(encoding="utf-8")

        immutable_tools = (
            "rhysd/actionlint:1.7.12@sha256:"
            "b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667",
            "zricethezav/gitleaks:v8.30.1@sha256:"
            "c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f",
            "aquasec/trivy:0.73.0@sha256:"
            "7cced7cae583819fc7806d4cbc0dbbc7cad18b99f7d3e235192e6da8c091045c",
        )
        for image in immutable_tools:
            with self.subTest(image=image):
                self.assertIn(image, workflow)

        self.assertEqual(workflow.count('--volume "$PWD:/repo:ro"'), 2)
        self.assertIn('--volume "$PWD:/mnt:ro"', workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn(
            '"$GITLEAKS_IMAGE" git --redact --no-banner --verbose /repo',
            workflow,
        )
        self.assertIn(
            "actions/dependency-review-action@"
            "a1d282b36b6f3519aa1f3fc636f609c47dddb294 # v5.0.0",
            workflow,
        )
        self.assertIn("if: github.event_name == 'pull_request'", workflow)
        self.assertIn("--severity HIGH,CRITICAL", workflow)
        self.assertIn("--exit-code 1", workflow)
        self.assertIn('"$TRIVY_IMAGE" image', workflow)
        self.assertIn("docker save --output", workflow)
        self.assertIn("--input", workflow)
        self.assertNotIn("/var/run/docker.sock", workflow)
        self.assertNotIn("docker login", workflow)
        self.assertNotIn("docker push", workflow)

    def test_dependabot_tracks_workflows_packages_and_container_manifests(self) -> None:
        config_path = REPOSITORY_ROOT / ".github" / "dependabot.yml"
        self.assertTrue(config_path.is_file(), ".github/dependabot.yml is required")
        config = config_path.read_text(encoding="utf-8")

        self.assertTrue(config.startswith("version: 2\n"))
        for ecosystem in (
            "github-actions",
            "gomod",
            "npm",
            "docker",
            "docker-compose",
        ):
            with self.subTest(ecosystem=ecosystem):
                self.assertIn(f'package-ecosystem: "{ecosystem}"', config)

        for directory in (
            "/camera-portal",
            "/camera-portal-lan/caddy",
            "/frigate-1panel",
            "/camera-portal-lan",
            "/examples/cloudflare",
        ):
            with self.subTest(directory=directory):
                self.assertIn(f'"{directory}"', config)

        self.assertIn('timezone: "Asia/Shanghai"', config)
        self.assertNotIn("registries:", config)
        self.assertNotIn("${{ secrets.", config)


if __name__ == "__main__":
    unittest.main()
