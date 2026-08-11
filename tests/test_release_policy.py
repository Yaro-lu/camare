from pathlib import Path
import re
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ReleasePolicyTests(unittest.TestCase):
    def test_release_is_only_triggered_by_version_tags_after_reusable_checks(self) -> None:
        workflow_path = REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"
        self.assertTrue(workflow_path.is_file(), "release workflow is required")
        workflow = workflow_path.read_text(encoding="utf-8")

        self.assertIn("    tags:\n      - 'v*'", workflow)
        self.assertNotIn("pull_request", workflow)
        self.assertNotIn("workflow_dispatch", workflow)
        self.assertIn("uses: ./.github/workflows/checks.yml", workflow)
        self.assertIn("needs: checks", workflow)

    def test_release_preflight_requires_an_annotated_semver_tag_on_main(self) -> None:
        script_path = REPOSITORY_ROOT / "scripts" / "release-check.sh"
        self.assertTrue(script_path.is_file(), "release policy script is required")
        script = script_path.read_text(encoding="utf-8")

        self.assertIn("preflight)", script)
        self.assertIn(
            "^v(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$",
            script,
        )
        self.assertIn('git cat-file -t "refs/tags/$tag"', script)
        self.assertIn('[[ "$object_type" == "tag" ]]', script)
        self.assertIn("git merge-base --is-ancestor", script)
        self.assertIn('"## [$version]"', script)
        self.assertIn("LICENSE", script)
        self.assertIn("THIRD_PARTY_NOTICES.md", script)
        self.assertNotIn("printenv", script)
        self.assertNotIn("set -x", script)

    def test_release_actions_are_immutable_and_use_job_scoped_permissions(self) -> None:
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("permissions: {}", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertNotIn("self-hosted", workflow)
        self.assertNotIn("ubuntu-latest", workflow)
        self.assertNotIn("${{ secrets.", workflow)
        self.assertNotIn("personal_access_token", workflow.lower())

        external_actions = re.findall(
            r"(?m)^\s*- uses: ((?!\./)[^\s]+)(?:\s+#\s*(.+))?$",
            workflow,
        )
        self.assertTrue(external_actions, "release workflow must use pinned actions")
        for action, version_comment in external_actions:
            with self.subTest(action=action):
                self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")
                self.assertRegex(version_comment, r"^v\d")

        checkout_count = sum(
            action.startswith("actions/checkout@") for action, _ in external_actions
        )
        self.assertGreater(checkout_count, 0)
        self.assertEqual(
            workflow.count("persist-credentials: false"),
            checkout_count,
        )

    def test_owned_images_are_built_and_scanned_by_platform_digest(self) -> None:
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")

        for image in (
            "ghcr.io/yaro-lu/camare-portal",
            "ghcr.io/yaro-lu/camare-lan-caddy",
        ):
            with self.subTest(image=image):
                self.assertIn(image, workflow)
        for context in ("./camera-portal", "./camera-portal-lan/caddy"):
            with self.subTest(context=context):
                self.assertIn(f"context: {context}", workflow)
        for platform in ("linux/amd64", "linux/arm64"):
            with self.subTest(platform=platform):
                self.assertIn(f"platform: {platform}", workflow)

        self.assertIn("runner: ubuntu-24.04-arm", workflow)
        self.assertIn("needs: validate", workflow)
        self.assertIn("sha-${{ github.sha }}-${{ matrix.arch }}", workflow)
        self.assertIn("push: true", workflow)
        self.assertIn("provenance: false", workflow)
        self.assertIn("sbom: false", workflow)
        self.assertIn(
            "aquasec/trivy:0.73.0@sha256:"
            "7cced7cae583819fc7806d4cbc0dbbc7cad18b99f7d3e235192e6da8c091045c",
            workflow,
        )
        self.assertIn('"$IMAGE_REPOSITORY@$IMAGE_DIGEST"', workflow)
        self.assertIn("--severity HIGH,CRITICAL", workflow)
        self.assertIn("--exit-code 1", workflow)
        self.assertIn("--format spdx-json", workflow)
        self.assertIn("actions/upload-artifact@", workflow)

    def test_manifest_check_requires_exactly_the_two_supported_platforms(self) -> None:
        script = (REPOSITORY_ROOT / "scripts" / "release-check.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("manifest)", script)
        self.assertIn("docker buildx imagetools inspect --raw", script)
        self.assertIn('platform.os == "linux"', script)
        self.assertIn('platform.architecture == "amd64"', script)
        self.assertIn('platform.architecture == "arm64"', script)
        self.assertIn("length == 2", script)
        self.assertIn('[[ "$actual_amd64" == "$expected_amd64" ]]', script)
        self.assertIn('[[ "$actual_arm64" == "$expected_arm64" ]]', script)

    def test_semver_tags_follow_scanned_sha_manifests_and_receive_attestations(self) -> None:
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("needs: build-platform", workflow)
        for artifact in (
            "release-portal-amd64",
            "release-portal-arm64",
            "release-lan-caddy-amd64",
            "release-lan-caddy-arm64",
        ):
            with self.subTest(artifact=artifact):
                self.assertIn(f"name: {artifact}", workflow)

        sha_manifest = 'candidate="$repository:sha-$GITHUB_SHA"'
        verify_manifest = "bash scripts/release-check.sh manifest"
        semver_manifest = 'semver_ref="$repository:$GITHUB_REF_NAME"'
        self.assertIn(sha_manifest, workflow)
        self.assertIn(verify_manifest, workflow)
        self.assertIn(semver_manifest, workflow)
        self.assertLess(workflow.index(sha_manifest), workflow.index(verify_manifest))
        self.assertLess(workflow.index(verify_manifest), workflow.index(semver_manifest))

        self.assertIn("actions/attest@", workflow)
        self.assertIn("subject-digest:", workflow)
        self.assertIn("sbom-path:", workflow)
        self.assertIn("push-to-registry: true", workflow)
        self.assertIn("image-digests.txt", workflow)
        self.assertIn("complete ARM deployment remains experimental", workflow)

        self.assertIn("environment: release", workflow)
        self.assertIn("artifact-metadata: write", workflow)
        self.assertIn("GHCR packages must be set to Public", workflow)
        self.assertIn("anonymous digest pulls", workflow)
        self.assertIn("Verify anonymous GHCR digest access", workflow)
        self.assertIn('DOCKER_CONFIG="$anonymous_docker_config"', workflow)
        self.assertIn(
            '"ghcr.io/yaro-lu/camare-portal@$PORTAL_DIGEST"', workflow
        )
        self.assertIn(
            '"ghcr.io/yaro-lu/camare-lan-caddy@$LAN_CADDY_DIGEST"', workflow
        )
        self.assertLess(
            workflow.index("Verify anonymous GHCR digest access"),
            workflow.index("Attest Portal provenance"),
        )
        for output in (
            "portal_amd64_digest",
            "portal_arm64_digest",
            "lan_caddy_amd64_digest",
            "lan_caddy_arm64_digest",
        ):
            with self.subTest(output=output):
                self.assertIn(f"steps.publish-images.outputs.{output}", workflow)

    def test_release_assets_are_deterministic_complete_and_checksum_verified(self) -> None:
        workflow = (
            REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        script = (REPOSITORY_ROOT / "scripts" / "release-check.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("git archive", workflow)
        self.assertIn("--format=tar", workflow)
        self.assertIn("gzip -n --best", workflow)
        self.assertIn("LC_ALL=C", workflow)
        self.assertIn("SHA256SUMS", workflow)
        self.assertIn("bash scripts/release-check.sh checksums", workflow)
        self.assertIn('gh release create "$GITHUB_REF_NAME"', workflow)
        self.assertIn("--verify-tag", workflow)
        self.assertIn("--notes-from-tag", workflow)
        self.assertNotIn("softprops/action-gh-release", workflow)
        self.assertNotIn("ncipollo/release-action", workflow)

        for asset in (
            "image-digests.txt",
            "camare-portal-linux-amd64.spdx.json",
            "camare-portal-linux-arm64.spdx.json",
            "camare-lan-caddy-linux-amd64.spdx.json",
            "camare-lan-caddy-linux-arm64.spdx.json",
        ):
            with self.subTest(asset=asset):
                self.assertIn(asset, workflow)

        self.assertIn("checksums)", script)
        self.assertIn("sha256sum --check --strict", script)
        self.assertIn("SHA256SUMS", script)
        self.assertIn("camare-$version.tar.gz", script)
        self.assertIn("image-digests.txt", script)
        self.assertIn(".spdx.json", script)


if __name__ == "__main__":
    unittest.main()
