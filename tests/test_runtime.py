import contextlib
import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from camarectl.layout import RuntimeLayout, create_staging_layout
from camarectl.runtime import (
    ApplyError,
    ApplyLockedError,
    ApplyResult,
    StagingValidation,
    apply_staging,
    validate_staging,
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_staging(
    parent: Path,
    marker: str,
    *,
    instance_id: str = "runtime-test",
    secret_names: tuple[str, ...] = ("lan-access-key", "camera-password-1"),
    intel: bool = False,
) -> RuntimeLayout:
    layout = create_staging_layout(parent, instance_id, include_intel=intel)
    layout.base_compose.write_text(
        f'name: "camare_{instance_id}"\nservices:\n  app:\n    image: app:{marker}\n',
        encoding="utf-8",
    )
    layout.env.write_text(f'RELEASE="{marker}"\n', encoding="utf-8")
    layout.frigate_config.write_text(
        f"version: {marker}\n", encoding="utf-8"
    )
    layout.caddyfile.write_text(f"# {marker}\n", encoding="utf-8")
    if layout.intel_compose is not None:
        layout.intel_compose.write_text(
            f"services:\n  app:\n    environment:\n      RELEASE: {marker}\n",
            encoding="utf-8",
        )
    for name in secret_names:
        secret = layout.secrets / name
        secret.write_text(f"{marker}-{name}-secret", encoding="utf-8")
        secret.chmod(0o600)
    non_secret = [
        layout.env,
        layout.base_compose,
        *(() if layout.intel_compose is None else (layout.intel_compose,)),
        layout.frigate_config,
        layout.caddyfile,
    ]
    manifest = {
        "format_version": 1,
        "instance_id": instance_id,
        "provider": "lan",
        "hardware": "intel-vaapi" if intel else "cpu",
        "build_mode": "local",
        "compose_files": [
            path.relative_to(layout.root).as_posix()
            for path in layout.compose_files
        ],
        "files": {
            path.relative_to(layout.root).as_posix(): digest(path)
            for path in non_secret
        },
        "required_ownership": {
            "portal-data": {"uid": 65532, "gid": 65532, "mode": "0750"}
        },
    }
    layout.manifest.write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    return layout


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Path, float]] = []

    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        timeout: float,
    ) -> str:
        self.calls.append((arguments, cwd, timeout))
        return "runner-output-that-must-not-be-printed"


class RuntimeApplyTests(unittest.TestCase):
    def test_public_staging_validation_is_read_only_and_returns_no_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = make_staging(root / "staging", "plan")
            raw_secret = (
                layout.secrets / "camera-password-1"
            ).read_text(encoding="utf-8")
            runner = RecordingRunner()
            output = io.StringIO()

            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = validate_staging(layout, runner, timeout=15)

            self.assertIsInstance(result, StagingValidation)
            self.assertEqual(result.root, layout.root.resolve())
            self.assertEqual(result.project_name, "camare_runtime-test")
            self.assertEqual(result.compose_files, (layout.base_compose.resolve(),))
            self.assertTrue(layout.root.is_dir())
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(runner.calls[0][0][-2:], ("config", "--quiet"))
            self.assertEqual(runner.calls[0][2], 15)
            self.assertNotIn(raw_secret, repr(result))
            self.assertEqual(output.getvalue(), "")

    def test_first_install_validates_staging_then_atomically_switches_and_starts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = make_staging(root / "staging", "v1")
            staging_root = layout.root
            target = root / "runtime"
            runner = RecordingRunner()
            ownership_calls: list[tuple[Path, int, int, int]] = []
            verified: list[Path] = []

            def ownership(path: Path, uid: int, gid: int, mode: int) -> None:
                ownership_calls.append((path, uid, gid, mode))

            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = apply_staging(
                    layout,
                    target,
                    runner,
                    verifier=lambda path: verified.append(path),
                    ownership=ownership,
                )

            self.assertIsInstance(result, ApplyResult)
            self.assertTrue(result.installed)
            self.assertTrue(result.changed)
            self.assertTrue(result.verified)
            self.assertEqual(result.target_root, target.resolve())
            self.assertFalse(staging_root.exists())
            self.assertEqual(
                (target / "frigate" / "config.yml").read_text(encoding="utf-8"),
                "version: v1\n",
            )
            self.assertEqual(
                ownership_calls,
                [(target / "portal-data", 65532, 65532, 0o750)],
            )
            self.assertEqual(verified, [target.resolve()])
            self.assertEqual(len(runner.calls), 2)
            validation, start = runner.calls
            self.assertEqual(validation[1], staging_root)
            self.assertIn("config", validation[0])
            self.assertIn("--quiet", validation[0])
            self.assertEqual(start[1], target.resolve())
            self.assertEqual(start[0][-3:], ("up", "-d", "--build"))
            self.assertTrue(all(timeout > 0 for _, _, timeout in runner.calls))
            self.assertEqual(output.getvalue(), "")

    def test_compose_validation_failure_does_not_touch_the_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = make_staging(root / "staging", "invalid")
            target = root / "runtime"
            target.mkdir()
            sentinel = target / "state.bin"
            sentinel.write_bytes(b"preserve-me")
            runner = RecordingRunner()

            def fail_validation(
                arguments: tuple[str, ...], *, cwd: Path, timeout: float
            ) -> None:
                runner(arguments, cwd=cwd, timeout=timeout)
                raise RuntimeError("compose rejected staging")

            with self.assertRaisesRegex(Exception, "validation failed"):
                apply_staging(
                    layout,
                    target,
                    fail_validation,
                    ownership=lambda *_: None,
                )

            self.assertEqual(sentinel.read_bytes(), b"preserve-me")
            self.assertEqual(set(target.iterdir()), {sentinel})
            self.assertTrue(layout.root.exists())
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(runner.calls[0][1], layout.root)

    def test_upgrade_replaces_only_controls_and_backs_up_obsolete_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "runtime"
            runner = RecordingRunner()
            ownership = lambda *_: None
            first = make_staging(
                root / "staging",
                "v1",
                secret_names=(
                    "lan-access-key",
                    "camera-password-1",
                    "cloudflare-token",
                ),
            )
            apply_staging(first, target, runner, ownership=ownership)
            portal_state = target / "portal-data" / "state.json"
            frigate_database = target / "frigate" / "frigate.db"
            frigate_auth = target / "frigate" / "auth.db"
            portal_state.write_bytes(b"bound-devices-v1")
            frigate_database.write_bytes(b"recording-index-v1")
            frigate_auth.write_bytes(b"frigate-users-v1")
            second = make_staging(
                root / "staging",
                "v2",
                secret_names=("lan-access-key", "camera-password-1"),
            )

            result = apply_staging(second, target, runner, ownership=ownership)

            self.assertFalse(result.installed)
            self.assertTrue(result.changed)
            self.assertIsNotNone(result.backup_root)
            self.assertFalse(second.root.exists())
            self.assertEqual(portal_state.read_bytes(), b"bound-devices-v1")
            self.assertEqual(frigate_database.read_bytes(), b"recording-index-v1")
            self.assertEqual(frigate_auth.read_bytes(), b"frigate-users-v1")
            self.assertEqual(
                (target / "frigate" / "config.yml").read_text(encoding="utf-8"),
                "version: v2\n",
            )
            self.assertFalse((target / "secrets" / "cloudflare-token").exists())
            backup = result.backup_root
            assert backup is not None
            self.assertTrue((backup / "secrets" / "cloudflare-token").is_file())
            self.assertFalse((backup / "portal-data").exists())
            self.assertFalse((backup / "frigate" / "frigate.db").exists())
            self.assertFalse((backup / "frigate" / "auth.db").exists())

    def test_identical_apply_is_idempotent_and_does_not_create_a_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "runtime"
            runner = RecordingRunner()
            first = make_staging(root / "staging", "same")
            apply_staging(first, target, runner, ownership=lambda *_: None)
            controls_before = {
                relative: (target / relative).read_bytes()
                for relative in (
                    ".env",
                    "compose.yaml",
                    "manifest.json",
                    "caddy/Caddyfile",
                    "frigate/config.yml",
                    "secrets/lan-access-key",
                    "secrets/camera-password-1",
                )
            }
            second = make_staging(root / "staging", "same")

            result = apply_staging(
                second,
                target,
                runner,
                ownership=lambda *_: None,
            )

            self.assertFalse(result.installed)
            self.assertFalse(result.changed)
            self.assertIsNone(result.backup_root)
            self.assertFalse(second.root.exists())
            self.assertFalse((target / ".camare-backups").exists())
            self.assertEqual(
                {
                    relative: (target / relative).read_bytes()
                    for relative in controls_before
                },
                controls_before,
            )

    def test_backups_rotate_to_three_and_removed_intel_override_is_preserved_in_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "runtime"
            runner = RecordingRunner()
            initial = make_staging(root / "staging", "v0", intel=True)
            apply_staging(initial, target, runner, ownership=lambda *_: None)
            first_upgrade = make_staging(root / "staging", "v1", intel=False)

            first_result = apply_staging(
                first_upgrade,
                target,
                runner,
                ownership=lambda *_: None,
            )

            self.assertFalse((target / "compose.intel.yaml").exists())
            assert first_result.backup_root is not None
            self.assertTrue(
                (first_result.backup_root / "compose.intel.yaml").is_file()
            )
            for version in ("v2", "v3", "v4"):
                staging = make_staging(root / "staging", version)
                apply_staging(staging, target, runner, ownership=lambda *_: None)

            backups = sorted((target / ".camare-backups").iterdir())
            self.assertEqual(len(backups), 3)
            self.assertTrue(all(path.name.startswith("backup-") for path in backups))
            self.assertTrue(all(not (path / "portal-data").exists() for path in backups))

    def test_upgrade_start_failure_restores_old_controls_and_old_compose_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "runtime"
            initial_runner = RecordingRunner()
            first = make_staging(root / "staging", "v1", intel=True)
            apply_staging(first, target, initial_runner, ownership=lambda *_: None)
            state = target / "portal-data" / "state.json"
            database = target / "frigate" / "frigate.db"
            state.write_bytes(b"paired-state")
            database.write_bytes(b"frigate-db")
            second = make_staging(root / "staging", "v2", intel=False)
            runner = RecordingRunner()
            up_attempts = 0

            def fail_new_start_once(
                arguments: tuple[str, ...], *, cwd: Path, timeout: float
            ) -> None:
                nonlocal up_attempts
                runner(arguments, cwd=cwd, timeout=timeout)
                if arguments[-3:] == ("up", "-d", "--build"):
                    up_attempts += 1
                    if up_attempts == 1:
                        raise RuntimeError("new project failed")

            with self.assertRaisesRegex(ApplyError, "restored"):
                apply_staging(
                    second,
                    target,
                    fail_new_start_once,
                    ownership=lambda *_: None,
                )

            self.assertEqual(
                (target / "frigate" / "config.yml").read_text(encoding="utf-8"),
                "version: v1\n",
            )
            self.assertTrue((target / "compose.intel.yaml").is_file())
            self.assertEqual(state.read_bytes(), b"paired-state")
            self.assertEqual(database.read_bytes(), b"frigate-db")
            self.assertTrue(second.root.exists())
            self.assertEqual(len(runner.calls), 3)
            self.assertEqual(runner.calls[0][0][-2:], ("config", "--quiet"))
            self.assertEqual(runner.calls[1][0][-3:], ("up", "-d", "--build"))
            self.assertEqual(
                runner.calls[2][0][-3:], ("up", "-d", "--no-build")
            )
            self.assertIn("compose.intel.yaml", " ".join(runner.calls[2][0]))

    def test_verifier_failure_restores_previous_runtime_without_exposing_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "runtime"
            apply_staging(
                make_staging(root / "staging", "v1"),
                target,
                RecordingRunner(),
                ownership=lambda *_: None,
            )
            second = make_staging(root / "staging", "v2")
            raw_secret = (
                second.secrets / "camera-password-1"
            ).read_text(encoding="utf-8")
            runner = RecordingRunner()
            output = io.StringIO()

            def fail_verification(_: Path) -> None:
                raise RuntimeError("media verification failed")

            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                with self.assertRaisesRegex(ApplyError, "restored"):
                    apply_staging(
                        second,
                        target,
                        runner,
                        verifier=fail_verification,
                        ownership=lambda *_: None,
                    )

            self.assertEqual(
                (target / "frigate" / "config.yml").read_text(encoding="utf-8"),
                "version: v1\n",
            )
            self.assertTrue(second.root.exists())
            self.assertEqual(
                [call[0][-3:] for call in runner.calls if "up" in call[0]],
                [("up", "-d", "--build"), ("up", "-d", "--no-build")],
            )
            self.assertNotIn(raw_secret, output.getvalue())

    def test_first_install_failure_stops_only_this_project_and_keeps_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "runtime"
            media = root / "external-media"
            media.mkdir()
            recording = media / "recording.mp4"
            recording.write_bytes(b"never-delete-media")
            layout = make_staging(root / "staging", "broken")
            runner = RecordingRunner()

            def fail_start(
                arguments: tuple[str, ...], *, cwd: Path, timeout: float
            ) -> None:
                runner(arguments, cwd=cwd, timeout=timeout)
                if arguments[-3:] == ("up", "-d", "--build"):
                    raise RuntimeError("start failed")

            with self.assertRaisesRegex(ApplyError, "diagnostic runtime"):
                apply_staging(
                    layout,
                    target,
                    fail_start,
                    ownership=lambda *_: None,
                )

            self.assertTrue(target.is_dir())
            self.assertTrue((target / "manifest.json").is_file())
            self.assertEqual(recording.read_bytes(), b"never-delete-media")
            self.assertEqual(runner.calls[-1][0][-1:], ("stop",))
            flattened = [argument for call, _, _ in runner.calls for argument in call]
            self.assertNotIn("down", flattened)
            self.assertNotIn("prune", flattened)
            self.assertNotIn("-v", flattened)

    def test_symlinked_target_root_is_rejected_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real_target = root / "real-runtime"
            real_target.mkdir()
            sentinel = real_target / "sentinel"
            sentinel.write_bytes(b"outside-target")
            target_link = root / "runtime-link"
            try:
                os.symlink(real_target, target_link, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlinks unavailable: {error}")
            layout = make_staging(root / "staging", "v1")
            runner = RecordingRunner()

            with self.assertRaisesRegex(ApplyError, "symlink"):
                apply_staging(
                    layout,
                    target_link,
                    runner,
                    ownership=lambda *_: None,
                )

            self.assertEqual(sentinel.read_bytes(), b"outside-target")
            self.assertEqual(runner.calls, [])

    def test_symlinked_staged_hash_or_secret_file_is_rejected_before_compose(self) -> None:
        for relative in (".env", "secrets/camera-password-1"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                layout = make_staging(root / "staging", "v1")
                managed = layout.root / relative
                external = root / "external-value"
                external.write_bytes(managed.read_bytes())
                managed.unlink()
                try:
                    os.symlink(external, managed)
                except OSError as error:
                    self.skipTest(f"file symlinks unavailable: {error}")
                runner = RecordingRunner()

                with self.assertRaisesRegex(ApplyError, "unsafe|symlink"):
                    apply_staging(
                        layout,
                        root / "runtime",
                        runner,
                        ownership=lambda *_: None,
                    )

                self.assertEqual(runner.calls, [])
                self.assertFalse((root / "runtime").exists())

    def test_nonregular_secret_entry_is_rejected_before_compose(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = make_staging(root / "staging", "v1")
            (layout.secrets / "nested-secret-directory").mkdir()
            runner = RecordingRunner()

            with self.assertRaisesRegex(ApplyError, "non-regular managed secret"):
                validate_staging(layout, runner)

            self.assertEqual(runner.calls, [])
            self.assertTrue(layout.root.is_dir())

    def test_manifest_rejects_nonportable_paths_before_compose(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = make_staging(root / "staging", "v1")
            manifest = json.loads(layout.manifest.read_text(encoding="utf-8"))
            manifest["compose_files"] = [r"..\outside.yaml"]
            layout.manifest.write_text(json.dumps(manifest), encoding="utf-8")
            runner = RecordingRunner()

            with self.assertRaisesRegex(ApplyError, "portable relative path"):
                validate_staging(layout, runner)

            self.assertEqual(runner.calls, [])
            self.assertTrue(layout.root.is_dir())

    def test_instance_lock_rejects_a_concurrent_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "runtime"
            first = make_staging(root / "staging", "v1")
            second = make_staging(root / "staging", "v2")
            entered_validation = threading.Event()
            release_validation = threading.Event()
            first_errors: list[BaseException] = []

            def blocking_runner(
                arguments: tuple[str, ...], *, cwd: Path, timeout: float
            ) -> None:
                if arguments[-2:] == ("config", "--quiet"):
                    entered_validation.set()
                    if not release_validation.wait(5):
                        raise RuntimeError("test timed out waiting to release validation")

            def first_apply() -> None:
                try:
                    apply_staging(
                        first,
                        target,
                        blocking_runner,
                        ownership=lambda *_: None,
                    )
                except BaseException as error:
                    first_errors.append(error)

            worker = threading.Thread(target=first_apply)
            worker.start()
            self.assertTrue(entered_validation.wait(5))
            try:
                with self.assertRaises(ApplyLockedError):
                    apply_staging(
                        second,
                        target,
                        RecordingRunner(),
                        ownership=lambda *_: None,
                    )
            finally:
                release_validation.set()
                worker.join(5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(first_errors, [])
            self.assertTrue(target.is_dir())
            self.assertTrue(second.root.is_dir())

    def test_backup_rotation_rejects_non_directory_or_symlinked_backup_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "runtime"
            apply_staging(
                make_staging(root / "staging", "v1"),
                target,
                RecordingRunner(),
                ownership=lambda *_: None,
            )
            backup_parent = target / ".camare-backups"
            backup_parent.mkdir()
            unsafe = backup_parent / "backup-not-a-directory"
            unsafe.write_bytes(b"must-not-be-followed-or-removed")
            staging = make_staging(root / "staging", "v2")

            with self.assertRaisesRegex(ApplyError, "unsafe backup"):
                apply_staging(
                    staging,
                    target,
                    RecordingRunner(),
                    ownership=lambda *_: None,
                )

            self.assertEqual(unsafe.read_bytes(), b"must-not-be-followed-or-removed")
            self.assertEqual(
                (target / "frigate" / "config.yml").read_text(encoding="utf-8"),
                "version: v1\n",
            )
            self.assertTrue(staging.root.exists())


if __name__ == "__main__":
    unittest.main()
