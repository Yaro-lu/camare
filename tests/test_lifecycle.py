from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path, PurePosixPath

from camarectl.lifecycle import backup_instance, restore_instance, uninstall_instance


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_sqlite(path: Path, value: str) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("create table state (value text not null)")
        connection.execute("insert into state values (?)", (value,))
        connection.commit()


def _wait_help_result() -> object:
    return type(
        "CommandResult",
        (),
        {"returncode": 0, "stdout": "  --wait  Wait for services\n", "stderr": ""},
    )()


def _set_strict_ownership_manifest(runtime: Path) -> None:
    (runtime / "secrets/lan-access-key").write_text(
        "lan-only-key", encoding="utf-8"
    )
    manifest_path = runtime / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["required_ownership"] = {
        "portal-data": {"uid": 65532, "gid": 65532, "mode": "0750"},
        "secrets": {"uid": 0, "gid": 0, "mode": "0700"},
        "secrets/lan-access-key": {"uid": 0, "gid": 65532, "mode": "0440"},
        "secrets/camera-password-1": {"uid": 0, "gid": 0, "mode": "0600"},
    }
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")


def _runtime(root: Path, instance_id: str = "home") -> tuple[Path, Path]:
    runtime = root / f"camare-{instance_id}"
    (runtime / "frigate").mkdir(parents=True)
    (runtime / "caddy").mkdir()
    (runtime / "portal-data").mkdir()
    (runtime / "secrets").mkdir()
    controls = {
        ".env": "CAMARE_INSTANCE_ID=home\nCAMARE_MEDIA_PATH=/outside/media\n",
        "compose.yaml": "services: {}\n",
        "frigate/config.yml": "record:\n  enabled: true\n",
        "caddy/Caddyfile": ":8080\n",
    }
    for relative, content in controls.items():
        path = runtime / relative
        path.write_text(content, encoding="utf-8")
    manifest = {
        "format_version": 2,
        "instance_id": instance_id,
        "provider": "lan",
        "compose_files": ["compose.yaml"],
        "files": {
            relative: _sha256(runtime / relative)
            for relative in controls
            if relative != "frigate/config.yml"
        },
        "mutable_files": ["frigate/config.yml"],
    }
    (runtime / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    (runtime / "portal-data/state.json").write_text(
        '{"devices":["phone"]}', encoding="utf-8"
    )
    _write_sqlite(runtime / "frigate/frigate.db", "recording-index")
    _write_sqlite(runtime / "frigate/auth.db", "administrator")
    (runtime / "secrets/camera-password-1").write_text(
        "do-not-back-up", encoding="utf-8"
    )
    media = root / "outside" / "media"
    media.mkdir(parents=True)
    (media / "recording.mp4").write_bytes(b"never-copy-media")
    return runtime, media


class BackupLifecycleTests(unittest.TestCase):
    def test_backup_contains_controls_and_state_but_never_secrets_or_media(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, media = _runtime(root)
            destination = root / "backups" / "home-20260811"

            result = backup_instance(
                runtime,
                destination,
                clock=lambda: "2026-08-11T04:00:00Z",
            )

            self.assertEqual(result.backup_root, destination)
            self.assertTrue((destination / "payload/control/compose.yaml").is_file())
            self.assertTrue((destination / "payload/portal/state.json").is_file())
            self.assertTrue((destination / "payload/frigate/frigate.db").is_file())
            self.assertFalse((destination / "payload/control/secrets").exists())
            backed_up_bytes = b"".join(
                path.read_bytes() for path in destination.rglob("*") if path.is_file()
            )
            self.assertNotIn(b"do-not-back-up", backed_up_bytes)
            self.assertNotIn(b"never-copy-media", backed_up_bytes)
            with closing(
                sqlite3.connect(destination / "payload/frigate/frigate.db")
            ) as connection:
                self.assertEqual(
                    connection.execute("select value from state").fetchone(),
                    ("recording-index",),
                )
            self.assertEqual((media / "recording.mp4").read_bytes(), b"never-copy-media")
            manifest = json.loads(
                (destination / "backup-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["instance_id"], "home")
            self.assertEqual(manifest["created_at"], "2026-08-11T04:00:00Z")
            self.assertEqual(
                manifest["required_secret_names"], ["camera-password-1"]
            )
            for artifact, entry in manifest["entries"].items():
                self.assertEqual(_sha256(destination / artifact), entry["sha256"])
            sidecar = (destination / "backup-manifest.sha256").read_text(
                encoding="ascii"
            )
            self.assertEqual(
                sidecar,
                f"{_sha256(destination / 'backup-manifest.json')}  backup-manifest.json\n",
            )

    def test_mutable_frigate_config_is_backed_up_after_admin_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            changed = "record:\n  output_args: preset-record-generic-audio-aac\n"
            (runtime / "frigate/config.yml").write_text(changed, encoding="utf-8")
            destination = root / "backups" / "after-admin-change"

            backup_instance(runtime, destination)

            self.assertEqual(
                (destination / "payload/control/frigate/config.yml").read_text(
                    encoding="utf-8"
                ),
                changed,
            )

    def test_failed_backup_is_not_published_and_partial_output_is_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            destination = root / "backups" / "failed"

            def fail_sqlite(_source: Path, output: Path) -> None:
                output.write_bytes(b"partial-sensitive-data")
                raise RuntimeError("injected backup failure")

            with self.assertRaisesRegex(Exception, "backup failed"):
                backup_instance(runtime, destination, sqlite_backup=fail_sqlite)

            self.assertFalse(destination.exists())
            self.assertEqual(list(destination.parent.glob(".failed.partial-*")), [])

    def test_running_sqlite_database_is_copied_as_a_consistent_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            source = runtime / "frigate/frigate.db"
            writer = sqlite3.connect(source)
            try:
                writer.execute("pragma journal_mode=wal")
                writer.execute("begin immediate")
                writer.execute("update state set value = 'not-committed'")

                destination = root / "backups" / "live-db"
                backup_instance(runtime, destination)

                with closing(
                    sqlite3.connect(destination / "payload/frigate/frigate.db")
                ) as snapshot:
                    self.assertEqual(
                        snapshot.execute("select value from state").fetchone(),
                        ("recording-index",),
                    )
            finally:
                writer.rollback()
                writer.close()

    def test_manifest_cannot_relabel_a_secret_as_a_control_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            manifest_path = runtime / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            secret = runtime / "secrets/camera-password-1"
            manifest["files"]["secrets/camera-password-1"] = _sha256(secret)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(Exception, "non-control path"):
                backup_instance(runtime, root / "backups" / "unsafe")

            self.assertFalse((root / "backups" / "unsafe").exists())

    def test_secret_value_in_runtime_env_is_not_copied_to_plain_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            env = runtime / ".env"
            env.write_text("CLOUDFLARE_TOKEN=plaintext-secret\n", encoding="utf-8")
            manifest_path = runtime / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"][".env"] = _sha256(env)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(Exception, "undeclared or duplicate key"):
                backup_instance(runtime, root / "backups" / "unsafe-env")

            self.assertFalse((root / "backups" / "unsafe-env").exists())

    def test_all_lifecycle_operations_share_the_runtime_apply_instance_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            known_good = root / "backups" / "known-good"
            backup_instance(runtime, known_good)
            entered = threading.Event()
            release = threading.Event()
            failures: list[BaseException] = []

            def slow_sqlite(source: Path, destination: Path) -> None:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release backup")
                with closing(sqlite3.connect(source)) as origin:
                    with closing(sqlite3.connect(destination)) as copy:
                        origin.backup(copy)

            def hold_lock() -> None:
                try:
                    backup_instance(
                        runtime,
                        root / "backups" / "slow",
                        sqlite_backup=slow_sqlite,
                    )
                except BaseException as error:  # reported on the test thread
                    failures.append(error)

            thread = threading.Thread(target=hold_lock, daemon=True)
            thread.start()
            self.assertTrue(entered.wait(5), "backup did not acquire the lock")
            try:
                with self.assertRaisesRegex(Exception, "instance is locked"):
                    backup_instance(runtime, root / "backups" / "second")
                with self.assertRaisesRegex(Exception, "instance is locked"):
                    restore_instance(known_good, runtime)
                with self.assertRaisesRegex(Exception, "instance is locked"):
                    uninstall_instance(runtime)
                self.assertTrue(
                    (runtime.parent / f".{runtime.name}.apply.lock").is_file()
                )
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])

    def test_precreated_lock_symlink_is_rejected_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            lock = runtime.parent / f".{runtime.name}.apply.lock"
            attacker_target = root / "attacker-owned"
            attacker_target.write_bytes(b"do-not-touch")
            try:
                lock.symlink_to(attacker_target)
            except OSError as error:
                self.skipTest(f"file symlinks unavailable: {error}")

            with self.assertRaisesRegex(Exception, "lock path.*unsafe"):
                backup_instance(runtime, root / "backups" / "unsafe-lock")

            self.assertEqual(attacker_target.read_bytes(), b"do-not-touch")
            self.assertFalse((root / "backups" / "unsafe-lock").exists())


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Path, float]] = []
        self.probes: list[tuple[tuple[str, ...], Path, float]] = []

    def __call__(
        self, arguments: tuple[str, ...], *, cwd: Path, timeout: float
    ) -> object:
        if arguments == ("docker", "compose", "up", "--help"):
            self.probes.append((arguments, cwd, timeout))
            return _wait_help_result()
        self.calls.append((arguments, cwd, timeout))
        return object()


class HelpOnlyRunner:
    def __init__(self, help_text: str) -> None:
        self.help_text = help_text
        self.calls: list[tuple[tuple[str, ...], Path, float]] = []

    def __call__(
        self, arguments: tuple[str, ...], *, cwd: Path, timeout: float
    ) -> object:
        self.calls.append((arguments, cwd, timeout))
        return type(
            "CommandResult",
            (),
            {"returncode": 0, "stdout": self.help_text, "stderr": ""},
        )()


class RestoreLifecycleTests(unittest.TestCase):
    def test_restore_requires_compose_wait_before_stopping_or_replacing_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            backup = root / "backups" / "known-good"
            backup_instance(runtime, backup)
            state = runtime / "portal-data/state.json"
            state.write_text('{"devices":["changed"]}', encoding="utf-8")
            before = state.read_bytes()
            runner = HelpOnlyRunner("Usage: docker compose up [OPTIONS]\n")

            with self.assertRaisesRegex(Exception, "--wait"):
                restore_instance(backup, runtime, runner=runner, apply=True)

            self.assertEqual(state.read_bytes(), before)
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(runner.calls[0][0], ("docker", "compose", "up", "--help"))

    def test_restore_is_a_validated_dry_run_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            backup = root / "backups" / "known-good"
            backup_instance(runtime, backup)
            (runtime / "portal-data/state.json").write_text(
                '{"devices":[]}', encoding="utf-8"
            )
            runner = FakeRunner()

            result = restore_instance(backup, runtime, runner=runner)

            self.assertFalse(result.applied)
            self.assertIn(PurePosixPath("portal-data/state.json"), result.paths)
            self.assertEqual(
                (runtime / "portal-data/state.json").read_text(encoding="utf-8"),
                '{"devices":[]}',
            )
            self.assertEqual(runner.calls, [])

    def test_apply_restores_state_after_stopping_writers_and_preserves_secrets_media(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, media = _runtime(root)
            backup = root / "backups" / "known-good"
            backup_instance(runtime, backup)
            (runtime / "portal-data/state.json").write_text(
                '{"devices":["changed"]}', encoding="utf-8"
            )
            (runtime / "secrets/camera-password-1").write_text(
                "current-secret", encoding="utf-8"
            )
            with closing(sqlite3.connect(runtime / "frigate/frigate.db")) as connection:
                connection.execute("update state set value = 'changed-index'")
                connection.commit()
            runner = FakeRunner()

            result = restore_instance(
                backup,
                runtime,
                runner=runner,
                apply=True,
            )

            self.assertTrue(result.applied)
            self.assertEqual(
                (runtime / "portal-data/state.json").read_text(encoding="utf-8"),
                '{"devices":["phone"]}',
            )
            with closing(sqlite3.connect(runtime / "frigate/frigate.db")) as connection:
                self.assertEqual(
                    connection.execute("select value from state").fetchone(),
                    ("recording-index",),
                )
            self.assertEqual(
                (runtime / "secrets/camera-password-1").read_text(encoding="utf-8"),
                "current-secret",
            )
            self.assertEqual((media / "recording.mp4").read_bytes(), b"never-copy-media")
            self.assertEqual(len(runner.calls), 2)
            self.assertEqual(runner.calls[0][0][-3:], ("stop", "camera-portal", "frigate"))
            self.assertEqual(
                runner.calls[1][0][-5:],
                ("up", "-d", "--no-build", "--wait", "--remove-orphans"),
            )
            self.assertNotIn("-v", runner.calls[0][0] + runner.calls[1][0])

    def test_restore_replays_strict_manifest_ownership_before_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            _set_strict_ownership_manifest(runtime)
            backup = root / "backups" / "known-good"
            backup_instance(runtime, backup)
            events: list[tuple[object, ...]] = []

            class OrderedRunner(FakeRunner):
                def __call__(
                    self,
                    arguments: tuple[str, ...],
                    *,
                    cwd: Path,
                    timeout: float,
                ) -> object:
                    result = super().__call__(arguments, cwd=cwd, timeout=timeout)
                    if arguments != ("docker", "compose", "up", "--help"):
                        events.append(("compose", *arguments[-5:]))
                    return result

            def record_ownership(path: Path, uid: int, gid: int, mode: int) -> None:
                events.append(
                    ("ownership", path.relative_to(runtime).as_posix(), uid, gid, mode)
                )

            restore_instance(
                backup,
                runtime,
                runner=OrderedRunner(),
                apply=True,
                ownership=record_ownership,
            )

            expected = {
                ("ownership", "portal-data", 65532, 65532, 0o750),
                ("ownership", "secrets", 0, 0, 0o700),
                ("ownership", "secrets/lan-access-key", 0, 65532, 0o440),
                ("ownership", "secrets/camera-password-1", 0, 0, 0o600),
            }
            ownership_events = {event for event in events if event[0] == "ownership"}
            self.assertEqual(ownership_events, expected)
            first_up = next(index for index, event in enumerate(events) if "up" in event)
            self.assertTrue(
                all(events.index(event) < first_up for event in ownership_events)
            )

    def test_restore_replays_restored_strict_and_rollback_legacy_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            _set_strict_ownership_manifest(runtime)
            backup = root / "backups" / "known-good"
            backup_instance(runtime, backup)

            manifest_path = runtime / "manifest.json"
            current = json.loads(manifest_path.read_text(encoding="utf-8"))
            current["required_ownership"] = {
                "portal-data": {"uid": 65532, "gid": 65532, "mode": "0750"}
            }
            manifest_path.write_text(json.dumps(current), encoding="utf-8")
            events: list[tuple[object, ...]] = []

            class OrderedRunner(FakeRunner):
                def __call__(
                    self,
                    arguments: tuple[str, ...],
                    *,
                    cwd: Path,
                    timeout: float,
                ) -> object:
                    result = super().__call__(arguments, cwd=cwd, timeout=timeout)
                    if arguments != ("docker", "compose", "up", "--help"):
                        events.append(("compose", *arguments[-5:]))
                    return result

            def record_ownership(path: Path, uid: int, gid: int, mode: int) -> None:
                events.append(
                    ("ownership", path.relative_to(runtime).as_posix(), uid, gid, mode)
                )

            with self.assertRaisesRegex(Exception, "verification failed"):
                restore_instance(
                    backup,
                    runtime,
                    runner=OrderedRunner(),
                    apply=True,
                    verifier=lambda _target: False,
                    ownership=record_ownership,
                )

            ownership_paths = [event[1] for event in events if event[0] == "ownership"]
            self.assertCountEqual(
                ownership_paths[:4],
                [
                    "portal-data",
                    "secrets",
                    "secrets/camera-password-1",
                    "secrets/lan-access-key",
                ],
            )
            self.assertEqual(ownership_paths[4:], ["portal-data"])
            up_positions = [
                index
                for index, event in enumerate(events)
                if event[0] == "compose" and "up" in event
            ]
            ownership_positions = [
                index
                for index, event in enumerate(events)
                if event[0] == "ownership"
            ]
            self.assertEqual(len(up_positions), 2)
            self.assertTrue(
                all(index < up_positions[0] for index in ownership_positions[:4])
            )
            self.assertLess(up_positions[0], ownership_positions[4])
            self.assertLess(ownership_positions[4], up_positions[1])

    def test_incomplete_strict_ownership_manifest_fails_before_probe_or_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            _set_strict_ownership_manifest(runtime)
            backup = root / "backups" / "known-good"
            backup_instance(runtime, backup)
            manifest_path = runtime / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            del manifest["required_ownership"]["secrets/lan-access-key"]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            before = manifest_path.read_bytes()
            runner = FakeRunner()

            with self.assertRaisesRegex(Exception, "ownership policy"):
                restore_instance(backup, runtime, runner=runner, apply=True)

            self.assertEqual(manifest_path.read_bytes(), before)
            self.assertEqual(runner.probes, [])
            self.assertEqual(runner.calls, [])

    @unittest.skipUnless(os.name == "posix", "POSIX ownership and modes")
    def test_restore_reapplies_safe_modes_and_parent_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            backup = root / "backups" / "known-good"
            backup_instance(runtime, backup)
            portal_parent = runtime / "portal-data"
            state = portal_parent / "state.json"
            state.chmod(0o600)
            (runtime / "caddy/Caddyfile").chmod(0o644)
            if os.geteuid() == 0:
                os.chown(portal_parent, 65532, 65532)
                os.chown(state, 65532, 65532)
            expected_owner = portal_parent.stat()

            restore_instance(backup, runtime, runner=FakeRunner(), apply=True)

            caddy = runtime / "caddy/Caddyfile"
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
            self.assertEqual(state.stat().st_uid, expected_owner.st_uid)
            self.assertEqual(state.stat().st_gid, expected_owner.st_gid)
            self.assertEqual(stat.S_IMODE(caddy.stat().st_mode), 0o644)

    @unittest.skipUnless(os.name == "posix", "POSIX ownership and modes")
    def test_restore_missing_state_uses_safe_mode_and_parent_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            backup = root / "backups" / "known-good"
            backup_instance(runtime, backup)
            state = runtime / "portal-data/state.json"
            state.unlink()
            portal_parent = runtime / "portal-data"
            if os.geteuid() == 0:
                os.chown(portal_parent, 65532, 65532)
            expected_owner = portal_parent.stat()

            restore_instance(backup, runtime, runner=FakeRunner(), apply=True)

            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
            self.assertEqual(state.stat().st_uid, expected_owner.st_uid)
            self.assertEqual(state.stat().st_gid, expected_owner.st_gid)

    def test_tampered_or_wrong_instance_backup_is_rejected_before_docker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            backup = root / "backups" / "known-good"
            backup_instance(runtime, backup)
            (backup / "payload/portal/state.json").write_text(
                '{"devices":["attacker"]}', encoding="utf-8"
            )
            runner = FakeRunner()

            with self.assertRaisesRegex(Exception, "hash or size mismatch"):
                restore_instance(backup, runtime, runner=runner, apply=True)
            self.assertEqual(runner.calls, [])

            clean = root / "backups" / "other-instance"
            other, _ = _runtime(root / "other-root", "other")
            backup_instance(other, clean)
            with self.assertRaisesRegex(Exception, "instance_id does not match"):
                restore_instance(clean, runtime, runner=runner, apply=True)
            self.assertEqual(runner.calls, [])

    def test_restore_fails_closed_when_required_secret_file_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            backup = root / "backups" / "known-good"
            backup_instance(runtime, backup)
            (runtime / "secrets/camera-password-1").unlink()
            runner = FakeRunner()

            with self.assertRaisesRegex(Exception, "required secret"):
                restore_instance(backup, runtime, runner=runner, apply=True)

            self.assertEqual(runner.calls, [])

    def test_failed_restore_rolls_back_all_files_and_restarts_previous_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            backup = root / "backups" / "known-good"
            backup_instance(runtime, backup)
            changed_state = '{"devices":["keep-current"]}'
            (runtime / "portal-data/state.json").write_text(
                changed_state, encoding="utf-8"
            )
            with closing(sqlite3.connect(runtime / "frigate/frigate.db")) as connection:
                connection.execute("update state set value = 'keep-current-index'")
                connection.commit()
            runner = FakeRunner()
            failed = False

            def fail_once(source: Path, destination: Path) -> None:
                nonlocal failed
                if destination.name == "state.json" and not failed:
                    failed = True
                    raise OSError("injected atomic replace failure")
                source.replace(destination)

            with self.assertRaisesRegex(Exception, "previous data was restored"):
                restore_instance(
                    backup,
                    runtime,
                    runner=runner,
                    apply=True,
                    replace=fail_once,
                )

            self.assertEqual(
                (runtime / "portal-data/state.json").read_text(encoding="utf-8"),
                changed_state,
            )
            with closing(sqlite3.connect(runtime / "frigate/frigate.db")) as connection:
                self.assertEqual(
                    connection.execute("select value from state").fetchone(),
                    ("keep-current-index",),
                )
            self.assertEqual(runner.calls[0][0][-3:], ("stop", "camera-portal", "frigate"))
            self.assertEqual(
                runner.calls[-1][0][-5:],
                ("up", "-d", "--no-build", "--wait", "--remove-orphans"),
            )

    def test_failed_post_start_verification_rolls_back_and_restarts_without_building(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            backup = root / "backups" / "known-good"
            backup_instance(runtime, backup)
            current_state = '{"devices":["current"]}'
            (runtime / "portal-data/state.json").write_text(
                current_state, encoding="utf-8"
            )
            runner = FakeRunner()
            verified_states: list[str] = []

            def reject_restored(target: Path) -> bool:
                verified_states.append(
                    (target / "portal-data/state.json").read_text(encoding="utf-8")
                )
                return False

            with self.assertRaisesRegex(Exception, "verification failed"):
                restore_instance(
                    backup,
                    runtime,
                    runner=runner,
                    apply=True,
                    verifier=reject_restored,
                )

            self.assertEqual(verified_states, ['{"devices":["phone"]}'])
            self.assertEqual(
                (runtime / "portal-data/state.json").read_text(encoding="utf-8"),
                current_state,
            )
            self.assertEqual(len(runner.calls), 4)
            self.assertEqual(
                runner.calls[1][0][-5:],
                ("up", "-d", "--no-build", "--wait", "--remove-orphans"),
            )
            self.assertEqual(
                runner.calls[2][0][-3:], ("stop", "camera-portal", "frigate")
            )
            self.assertEqual(
                runner.calls[3][0][-5:],
                ("up", "-d", "--no-build", "--wait", "--remove-orphans"),
            )

    def test_uncertain_stop_failure_reasserts_previous_project_without_building(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            backup = root / "backups" / "known-good"
            backup_instance(runtime, backup)
            calls: list[tuple[str, ...]] = []

            def fail_stop(
                arguments: tuple[str, ...], *, cwd: Path, timeout: float
            ) -> object:
                if arguments == ("docker", "compose", "up", "--help"):
                    return _wait_help_result()
                calls.append(arguments)
                if "stop" in arguments:
                    raise RuntimeError("partial Docker stop")
                return object()

            with self.assertRaisesRegex(Exception, "writers could not be stopped"):
                restore_instance(backup, runtime, runner=fail_stop, apply=True)

            self.assertEqual(calls[0][-3:], ("stop", "camera-portal", "frigate"))
            self.assertEqual(
                calls[1][-5:],
                ("up", "-d", "--no-build", "--wait", "--remove-orphans"),
            )
            self.assertEqual(
                (runtime / "portal-data/state.json").read_text(encoding="utf-8"),
                '{"devices":["phone"]}',
            )

    def test_uncertain_restored_start_is_stopped_before_old_data_is_put_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            backup = root / "backups" / "known-good"
            backup_instance(runtime, backup)
            current_state = '{"devices":["current"]}'
            (runtime / "portal-data/state.json").write_text(
                current_state, encoding="utf-8"
            )
            calls: list[tuple[str, ...]] = []

            def fail_first_start(
                arguments: tuple[str, ...], *, cwd: Path, timeout: float
            ) -> object:
                if arguments == ("docker", "compose", "up", "--help"):
                    return _wait_help_result()
                calls.append(arguments)
                if (
                    arguments[-5:]
                    == ("up", "-d", "--no-build", "--wait", "--remove-orphans")
                    and len(calls) == 2
                ):
                    raise RuntimeError("uncertain restored start")
                return object()

            with self.assertRaisesRegex(Exception, "previous data was restored"):
                restore_instance(backup, runtime, runner=fail_first_start, apply=True)

            self.assertEqual(calls[0][-3:], ("stop", "camera-portal", "frigate"))
            self.assertEqual(
                calls[1][-5:],
                ("up", "-d", "--no-build", "--wait", "--remove-orphans"),
            )
            self.assertEqual(calls[2][-3:], ("stop", "camera-portal", "frigate"))
            self.assertEqual(
                calls[3][-5:],
                ("up", "-d", "--no-build", "--wait", "--remove-orphans"),
            )
            self.assertEqual(
                (runtime / "portal-data/state.json").read_text(encoding="utf-8"),
                current_state,
            )

    def test_failed_restore_recovery_preserves_the_only_rollback_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            backup = root / "backups" / "known-good"
            backup_instance(runtime, backup)

            def fail_state_replacement(source: Path, destination: Path) -> None:
                if destination.name == "state.json":
                    raise OSError("cannot replace state")
                source.replace(destination)

            with self.assertRaisesRegex(Exception, "recovery copy preserved"):
                restore_instance(
                    backup,
                    runtime,
                    runner=FakeRunner(),
                    apply=True,
                    replace=fail_state_replacement,
                )

            recovery = list(root.glob(".camare-home.restore-rollback-*"))
            self.assertEqual(len(recovery), 1)
            self.assertTrue((recovery[0] / "portal-data/state.json").is_file())

    def test_restore_recovers_files_before_propagating_keyboard_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            backup = root / "backups" / "known-good"
            backup_instance(runtime, backup)
            original = '{"devices":["current"]}'
            (runtime / "portal-data/state.json").write_text(original, encoding="utf-8")
            interrupted = False

            def interrupt_once(source: Path, destination: Path) -> None:
                nonlocal interrupted
                if destination.name == "state.json" and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt()
                source.replace(destination)

            with self.assertRaises(KeyboardInterrupt):
                restore_instance(
                    backup,
                    runtime,
                    runner=FakeRunner(),
                    apply=True,
                    replace=interrupt_once,
                )

            self.assertEqual(
                (runtime / "portal-data/state.json").read_text(encoding="utf-8"),
                original,
            )
            self.assertEqual(list(root.glob(".camare-home.restore-rollback-*")), [])


class UninstallLifecycleTests(unittest.TestCase):
    def test_uninstall_requires_compose_wait_before_down_or_file_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, _ = _runtime(root)
            runner = HelpOnlyRunner("Usage: docker compose up [OPTIONS]\n")

            with self.assertRaisesRegex(Exception, "--wait"):
                uninstall_instance(
                    runtime,
                    runner=runner,
                    apply=True,
                    purge_data=True,
                )

            self.assertTrue((runtime / "compose.yaml").is_file())
            self.assertTrue((runtime / "manifest.json").is_file())
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(runner.calls[0][0], ("docker", "compose", "up", "--help"))

    def test_uninstall_is_a_non_mutating_instance_scoped_plan_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, media = _runtime(root)
            runner = FakeRunner()

            result = uninstall_instance(runtime, runner=runner)

            self.assertFalse(result.applied)
            self.assertFalse(result.purge_data)
            self.assertEqual(result.project_name, "camare_home")
            self.assertIn(PurePosixPath("compose.yaml"), result.control_paths)
            self.assertIn(PurePosixPath("secrets"), result.control_paths)
            self.assertNotIn(
                PurePosixPath("portal-data/state.json"), result.control_paths
            )
            self.assertTrue((runtime / "compose.yaml").is_file())
            self.assertTrue((runtime / "portal-data/state.json").is_file())
            self.assertTrue((runtime / "frigate/frigate.db").is_file())
            self.assertEqual((media / "recording.mp4").read_bytes(), b"never-copy-media")
            self.assertEqual(runner.calls, [])

    def test_apply_default_removes_only_project_and_preserves_reinstallable_instance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, media = _runtime(root)
            old_secret = runtime / ".camare-backups/backup-1/secrets/password"
            old_secret.parent.mkdir(parents=True)
            old_secret.write_text("old-secret", encoding="utf-8")
            runner = FakeRunner()

            result = uninstall_instance(runtime, runner=runner, apply=True)

            self.assertTrue(result.applied)
            self.assertTrue((runtime / "compose.yaml").is_file())
            self.assertTrue((runtime / ".env").is_file())
            self.assertTrue((runtime / "manifest.json").is_file())
            self.assertTrue((runtime / "secrets/camera-password-1").is_file())
            self.assertTrue((runtime / ".camare-backups").is_dir())
            self.assertTrue((runtime / "frigate/config.yml").is_file())
            self.assertTrue((runtime / "portal-data/state.json").is_file())
            self.assertTrue((runtime / "frigate/frigate.db").is_file())
            self.assertTrue((runtime / "frigate/auth.db").is_file())
            self.assertEqual((media / "recording.mp4").read_bytes(), b"never-copy-media")
            self.assertEqual(len(runner.calls), 1)
            arguments = runner.calls[0][0]
            self.assertIn("camare_home", arguments)
            self.assertEqual(arguments[-2:], ("down", "--remove-orphans"))
            self.assertNotIn("-v", arguments)
            self.assertNotIn("--volumes", arguments)
            self.assertNotIn("prune", arguments)

    def test_persistent_data_is_deleted_only_with_explicit_purge_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, media = _runtime(root)
            old_secret = runtime / ".camare-backups/backup-1/secrets/password"
            old_secret.parent.mkdir(parents=True)
            old_secret.write_text("old-secret", encoding="utf-8")
            runner = FakeRunner()

            result = uninstall_instance(
                runtime,
                runner=runner,
                apply=True,
                purge_data=True,
            )

            self.assertTrue(result.purge_data)
            self.assertIn(PurePosixPath("portal-data/state.json"), result.data_paths)
            self.assertIn(PurePosixPath("frigate/frigate.db"), result.data_paths)
            self.assertFalse((runtime / "portal-data/state.json").exists())
            self.assertFalse((runtime / "frigate/frigate.db").exists())
            self.assertFalse((runtime / "frigate/auth.db").exists())
            self.assertFalse((runtime / "compose.yaml").exists())
            self.assertFalse((runtime / "manifest.json").exists())
            self.assertFalse((runtime / "secrets").exists())
            self.assertFalse((runtime / ".camare-backups").exists())
            self.assertEqual((media / "recording.mp4").read_bytes(), b"never-copy-media")

    def test_failed_container_removal_does_not_touch_any_instance_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, media = _runtime(root)

            def fail_runner(
                arguments: tuple[str, ...], *, cwd: Path, timeout: float
            ) -> object:
                if arguments == ("docker", "compose", "up", "--help"):
                    return _wait_help_result()
                raise RuntimeError(f"Docker unavailable for {arguments[-1]}")

            with self.assertRaisesRegex(Exception, "containers could not be removed"):
                uninstall_instance(runtime, runner=fail_runner, apply=True)

            self.assertTrue((runtime / "manifest.json").is_file())
            self.assertTrue((runtime / "secrets/camera-password-1").is_file())
            self.assertTrue((runtime / "portal-data/state.json").is_file())
            self.assertTrue((runtime / "frigate/frigate.db").is_file())
            self.assertEqual((media / "recording.mp4").read_bytes(), b"never-copy-media")

    def test_failed_control_removal_rolls_back_and_restarts_without_building(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, media = _runtime(root)
            _set_strict_ownership_manifest(runtime)
            runner = FakeRunner()
            failed = False
            ownership_calls: list[tuple[str, int, int, int]] = []

            def fail_once(source: Path, destination: Path) -> None:
                nonlocal failed
                if source.name == "compose.yaml" and not failed:
                    failed = True
                    raise OSError("injected quarantine move failure")
                source.replace(destination)

            def record_ownership(path: Path, uid: int, gid: int, mode: int) -> None:
                ownership_calls.append(
                    (path.relative_to(runtime).as_posix(), uid, gid, mode)
                )

            with self.assertRaisesRegex(Exception, "previous instance was restored"):
                uninstall_instance(
                    runtime,
                    runner=runner,
                    apply=True,
                    purge_data=True,
                    replace=fail_once,
                    ownership=record_ownership,
                )

            self.assertTrue((runtime / "manifest.json").is_file())
            self.assertTrue((runtime / "compose.yaml").is_file())
            self.assertTrue((runtime / "secrets/camera-password-1").is_file())
            self.assertTrue((runtime / "portal-data/state.json").is_file())
            self.assertEqual((media / "recording.mp4").read_bytes(), b"never-copy-media")
            self.assertEqual(len(runner.calls), 2)
            self.assertEqual(runner.calls[0][0][-2:], ("down", "--remove-orphans"))
            self.assertEqual(
                runner.calls[1][0][-5:],
                ("up", "-d", "--no-build", "--wait", "--remove-orphans"),
            )
            self.assertEqual(
                set(ownership_calls),
                {
                    ("portal-data", 65532, 65532, 0o750),
                    ("secrets", 0, 0, 0o700),
                    ("secrets/camera-password-1", 0, 0, 0o600),
                    ("secrets/lan-access-key", 0, 65532, 0o440),
                },
            )

    def test_failed_purge_recovery_preserves_the_quarantined_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, media = _runtime(root)
            calls = 0

            def fail_move_and_recovery(source: Path, destination: Path) -> None:
                nonlocal calls
                calls += 1
                if calls in {2, 3}:
                    raise OSError("injected move and recovery failure")
                source.replace(destination)

            with self.assertRaisesRegex(Exception, "recovery copy preserved"):
                uninstall_instance(
                    runtime,
                    runner=FakeRunner(),
                    apply=True,
                    purge_data=True,
                    replace=fail_move_and_recovery,
                )

            recovery = list(root.glob(".camare-home.uninstall-*"))
            self.assertEqual(len(recovery), 1)
            self.assertTrue(any(path.is_file() for path in recovery[0].rglob("*")))
            self.assertEqual((media / "recording.mp4").read_bytes(), b"never-copy-media")

    def test_purge_recovers_files_before_propagating_keyboard_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime, media = _runtime(root)
            calls = 0

            def interrupt_once(source: Path, destination: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise KeyboardInterrupt()
                source.replace(destination)

            with self.assertRaises(KeyboardInterrupt):
                uninstall_instance(
                    runtime,
                    runner=FakeRunner(),
                    apply=True,
                    purge_data=True,
                    replace=interrupt_once,
                )

            self.assertTrue((runtime / ".env").is_file())
            self.assertTrue((runtime / "manifest.json").is_file())
            self.assertEqual(list(root.glob(".camare-home.uninstall-*")), [])
            self.assertEqual((media / "recording.mp4").read_bytes(), b"never-copy-media")


if __name__ == "__main__":
    unittest.main()
