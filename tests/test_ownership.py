import unittest
import tempfile
from pathlib import Path
from pathlib import PurePosixPath

from camarectl.ownership import (
    OwnershipPolicyError,
    OwnershipRequirement,
    parse_required_ownership,
    replay_required_ownership,
)


def cloudflare_manifest() -> dict[str, object]:
    return {
        "provider": "cloudflare",
        "required_ownership": {
            "portal-data": {"uid": 65532, "gid": 65532, "mode": "0750"},
            "secrets": {"uid": 0, "gid": 0, "mode": "0700"},
            "secrets/lan-access-key": {
                "uid": 0,
                "gid": 65532,
                "mode": "0440",
            },
            "secrets/camera-password-1": {
                "uid": 0,
                "gid": 0,
                "mode": "0600",
            },
            "secrets/camera-password-2": {
                "uid": 0,
                "gid": 0,
                "mode": "0600",
            },
            "secrets/cloudflare-token": {
                "uid": 65532,
                "gid": 65532,
                "mode": "0400",
            },
        },
    }


class OwnershipPolicyTests(unittest.TestCase):
    def test_cloudflare_policy_has_exact_consumer_identities_and_safe_directory(self) -> None:
        policy = parse_required_ownership(cloudflare_manifest())

        self.assertFalse(policy.legacy)
        by_path = {
            requirement.path: requirement for requirement in policy.requirements
        }
        self.assertEqual(
            (by_path[PurePosixPath("secrets")].uid,
             by_path[PurePosixPath("secrets")].gid,
             by_path[PurePosixPath("secrets")].mode),
            (0, 0, 0o700),
        )
        self.assertEqual(
            (by_path[PurePosixPath("secrets/lan-access-key")].uid,
             by_path[PurePosixPath("secrets/lan-access-key")].gid,
             by_path[PurePosixPath("secrets/lan-access-key")].mode),
            (0, 65532, 0o440),
        )
        self.assertEqual(
            (by_path[PurePosixPath("secrets/cloudflare-token")].uid,
             by_path[PurePosixPath("secrets/cloudflare-token")].gid,
             by_path[PurePosixPath("secrets/cloudflare-token")].mode),
            (65532, 65532, 0o400),
        )

    def test_legacy_policy_requires_explicit_compatibility_mode(self) -> None:
        manifest = {
            "provider": "lan",
            "required_ownership": {
                "portal-data": {
                    "uid": 65532,
                    "gid": 65532,
                    "mode": "0750",
                },
            },
        }

        with self.assertRaisesRegex(OwnershipPolicyError, "legacy"):
            parse_required_ownership(manifest)

        policy = parse_required_ownership(manifest, allow_legacy=True)
        self.assertTrue(policy.legacy)
        self.assertEqual(len(policy.requirements), 1)
        requirement = policy.requirements[0]
        self.assertEqual(requirement.path, PurePosixPath("portal-data"))
        self.assertEqual(
            (requirement.uid, requirement.gid, requirement.mode),
            (65532, 65532, 0o750),
        )

    def test_missing_legacy_policy_is_empty_only_in_compatibility_mode(self) -> None:
        manifest = {"provider": "lan"}

        with self.assertRaisesRegex(OwnershipPolicyError, "legacy"):
            parse_required_ownership(manifest)

        policy = parse_required_ownership(manifest, allow_legacy=True)
        self.assertTrue(policy.legacy)
        self.assertEqual(policy.requirements, ())

    def test_replay_applies_requirements_only_to_safe_existing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "secrets").mkdir()
            secret = root / "secrets" / "lan-access-key"
            secret.write_text("opaque", encoding="utf-8")
            calls: list[tuple[Path, int, int, int]] = []
            requirements = (
                OwnershipRequirement(PurePosixPath("secrets"), 0, 0, 0o700),
                OwnershipRequirement(
                    PurePosixPath("secrets/lan-access-key"),
                    0,
                    65532,
                    0o440,
                ),
            )

            replay_required_ownership(
                root,
                requirements,
                lambda path, uid, gid, mode: calls.append(
                    (path, uid, gid, mode)
                ),
            )

            self.assertEqual(
                calls,
                [
                    (root / "secrets", 0, 0, 0o700),
                    (secret, 0, 65532, 0o440),
                ],
            )
            with self.assertRaisesRegex(OwnershipPolicyError, "missing or unsafe"):
                replay_required_ownership(
                    root,
                    (
                        OwnershipRequirement(
                            PurePosixPath("secrets/missing"), 0, 0, 0o600
                        ),
                    ),
                    lambda *_: None,
                )

if __name__ == "__main__":
    unittest.main()
