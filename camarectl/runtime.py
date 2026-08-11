"""Transactional application of a rendered camare staging directory."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Protocol

from .layout import RuntimeLayout


class ApplyError(RuntimeError):
    """The staging tree could not be applied safely."""


class ApplyLockedError(ApplyError):
    """Another apply operation currently owns the instance lock."""


class CommandRunner(Protocol):
    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        timeout: float,
    ) -> object: ...


Verifier = Callable[[Path], bool | None]
OwnershipSetter = Callable[[Path, int, int, int], None]


@dataclass(frozen=True)
class ApplyResult:
    target_root: Path
    project_name: str
    installed: bool
    changed: bool
    verified: bool
    backup_root: Path | None = None


@dataclass(frozen=True)
class StagingValidation:
    """Non-secret metadata returned after read-only staging validation."""

    root: Path
    project_name: str
    compose_files: tuple[Path, ...]


@dataclass(frozen=True)
class _Manifest:
    instance_id: str
    compose_files: tuple[PurePosixPath, ...]
    required_ownership: tuple[tuple[PurePosixPath, int, int, int], ...]

    @property
    def project_name(self) -> str:
        return f"camare_{self.instance_id}"


SAFE_INSTANCE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _is_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_path(value: object, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ApplyError(f"{label} must be a non-empty relative path")
    if "\\" in value or ":" in value or "\x00" in value:
        raise ApplyError(f"{label} must be a portable relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ApplyError(f"{label} must stay inside the runtime root")
    return path


def _local_path(root: Path, relative: PurePosixPath) -> Path:
    return root.joinpath(*relative.parts)


def _load_manifest(root: Path) -> _Manifest:
    manifest_path = root / "manifest.json"
    if _is_link(root) or _is_link(manifest_path):
        raise ApplyError("runtime root or manifest is an unsafe symlink")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ApplyError("runtime manifest is missing or invalid") from error
    if not isinstance(document, dict):
        raise ApplyError("runtime manifest must be an object")
    instance_id = document.get("instance_id")
    if not isinstance(instance_id, str) or not SAFE_INSTANCE_ID.fullmatch(instance_id):
        raise ApplyError("runtime manifest has an unsafe instance_id")

    raw_compose = document.get("compose_files")
    if not isinstance(raw_compose, list) or not raw_compose:
        raise ApplyError("runtime manifest must list compose_files")
    compose_files = tuple(
        _relative_path(value, "compose file") for value in raw_compose
    )
    if len(set(compose_files)) != len(compose_files):
        raise ApplyError("runtime manifest contains duplicate compose_files")
    for relative in compose_files:
        compose_path = _local_path(root, relative)
        if _is_link(compose_path) or not compose_path.is_file():
            raise ApplyError(f"runtime compose file is missing: {relative}")

    raw_files = document.get("files")
    if not isinstance(raw_files, dict) or not raw_files:
        raise ApplyError("runtime manifest must contain non-secret file hashes")
    for raw_relative, expected in raw_files.items():
        relative = _relative_path(raw_relative, "manifest file")
        path = _local_path(root, relative)
        if (
            not isinstance(expected, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected)
            or _is_link(path)
            or not path.is_file()
            or _digest(path) != expected
        ):
            raise ApplyError(f"runtime file hash mismatch: {relative}")

    raw_ownership = document.get("required_ownership", {})
    if not isinstance(raw_ownership, dict):
        raise ApplyError("required_ownership must be an object")
    ownership: list[tuple[PurePosixPath, int, int, int]] = []
    for raw_relative, raw_requirement in raw_ownership.items():
        relative = _relative_path(raw_relative, "ownership path")
        if not isinstance(raw_requirement, dict):
            raise ApplyError(f"invalid ownership requirement: {relative}")
        uid = raw_requirement.get("uid")
        gid = raw_requirement.get("gid")
        raw_mode = raw_requirement.get("mode")
        try:
            mode = int(raw_mode, 8)
        except (TypeError, ValueError) as error:
            raise ApplyError(f"invalid ownership mode: {relative}") from error
        if (
            not isinstance(uid, int)
            or isinstance(uid, bool)
            or uid < 0
            or not isinstance(gid, int)
            or isinstance(gid, bool)
            or gid < 0
            or not 0 <= mode <= 0o777
        ):
            raise ApplyError(f"invalid ownership requirement: {relative}")
        ownership.append((relative, uid, gid, mode))
    return _Manifest(
        instance_id=instance_id,
        compose_files=compose_files,
        required_ownership=tuple(ownership),
    )


def _compose_arguments(
    root: Path,
    manifest: _Manifest,
    action: tuple[str, ...],
) -> tuple[str, ...]:
    arguments = [
        "docker",
        "compose",
        "--project-name",
        manifest.project_name,
        "--env-file",
        str(root / ".env"),
    ]
    for relative in manifest.compose_files:
        arguments.extend(("-f", str(_local_path(root, relative))))
    arguments.extend(action)
    return tuple(arguments)


def _run(
    runner: CommandRunner,
    root: Path,
    manifest: _Manifest,
    action: tuple[str, ...],
    timeout: float,
) -> object:
    return runner(
        _compose_arguments(root, manifest, action),
        cwd=root,
        timeout=timeout,
    )


def _validate_staging(
    layout: RuntimeLayout,
    runner: CommandRunner,
    timeout: float,
) -> tuple[Path, _Manifest]:
    if timeout <= 0:
        raise ApplyError("timeout must be greater than zero")
    raw_root = layout.root.expanduser().absolute()
    if _is_link(raw_root):
        raise ApplyError("staging runtime is an unsafe symlink")
    root = raw_root.resolve()
    if not root.is_dir():
        raise ApplyError("staging runtime does not exist")
    _control_files(root)
    manifest = _load_manifest(root)
    try:
        _run(runner, root, manifest, ("config", "--quiet"), timeout)
    except Exception as error:
        raise ApplyError("staging compose validation failed") from error
    return root, manifest


def validate_staging(
    layout: RuntimeLayout,
    runner: CommandRunner,
    *,
    timeout: float = 120.0,
) -> StagingValidation:
    """Validate rendered controls and Compose without changing the staging tree."""

    root, manifest = _validate_staging(layout, runner, timeout)
    return StagingValidation(
        root=root,
        project_name=manifest.project_name,
        compose_files=tuple(
            _local_path(root, relative) for relative in manifest.compose_files
        ),
    )


def _default_ownership(path: Path, uid: int, gid: int, mode: int) -> None:
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise ApplyError("applying runtime ownership requires Linux root")
    os.chown(path, uid, gid)
    path.chmod(mode)


def _apply_ownership(
    root: Path,
    manifest: _Manifest,
    ownership: OwnershipSetter,
) -> None:
    for relative, uid, gid, mode in manifest.required_ownership:
        path = _local_path(root, relative)
        if not path.exists() or _is_link(path):
            raise ApplyError(f"ownership target is missing or unsafe: {relative}")
        ownership(path, uid, gid, mode)


@contextmanager
def _instance_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _is_link(path):
        raise ApplyError("instance lock is an unsafe symlink")
    handle = path.open("a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise ApplyLockedError("another apply owns the instance lock") from error
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise ApplyLockedError("another apply owns the instance lock") from error
        locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _verify(verifier: Verifier | None, target: Path) -> bool:
    if verifier is None:
        return False
    result = verifier(target)
    if result is False:
        raise ApplyError("runtime verification failed")
    return True


def _control_files(root: Path) -> frozenset[PurePosixPath]:
    for directory_name in ("caddy", "frigate", "secrets"):
        directory = root / directory_name
        if _is_link(directory) or (directory.exists() and not directory.is_dir()):
            raise ApplyError(f"unsafe managed directory: {directory_name}")
    paths: set[PurePosixPath] = set()
    for path in root.glob("compose*.yaml"):
        if _is_link(path) or not path.is_file():
            raise ApplyError(f"unsafe managed compose file: {path.name}")
        paths.add(PurePosixPath(path.name))
    for relative in (
        PurePosixPath(".env"),
        PurePosixPath("manifest.json"),
        PurePosixPath("caddy/Caddyfile"),
        PurePosixPath("frigate/config.yml"),
    ):
        path = _local_path(root, relative)
        if _is_link(path):
            raise ApplyError(f"unsafe managed file: {relative}")
        if path.is_file():
            paths.add(relative)
    secrets = root / "secrets"
    if secrets.is_dir():
        for path in secrets.rglob("*"):
            if _is_link(path):
                raise ApplyError(
                    f"unsafe managed secret: {path.relative_to(root).as_posix()}"
                )
            if not path.is_file():
                raise ApplyError(
                    f"non-regular managed secret: "
                    f"{path.relative_to(root).as_posix()}"
                )
            paths.add(PurePosixPath(path.relative_to(root).as_posix()))
    return frozenset(paths)


def _controls_equal(left: Path, right: Path) -> bool:
    left_files = _control_files(left)
    right_files = _control_files(right)
    if left_files != right_files:
        return False
    return all(
        _digest(_local_path(left, relative))
        == _digest(_local_path(right, relative))
        for relative in left_files
    )


def _copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.camare-new-",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_controls(source: Path, destination: Path) -> None:
    old_files = _control_files(destination)
    new_files = _control_files(source)
    for relative in sorted(new_files):
        _copy_atomic(
            _local_path(source, relative),
            _local_path(destination, relative),
        )
    for relative in sorted(old_files - new_files, reverse=True):
        _local_path(destination, relative).unlink(missing_ok=True)


def _create_backup(target: Path) -> Path:
    backup_parent = target / ".camare-backups"
    if _is_link(backup_parent):
        raise ApplyError("unsafe backup directory")
    backup_parent.mkdir(mode=0o700, exist_ok=True)
    existing_backups: list[Path] = []
    for path in backup_parent.iterdir():
        if not path.name.startswith("backup-"):
            continue
        if _is_link(path) or not path.is_dir():
            raise ApplyError(f"unsafe backup entry: {path.name}")
        for child in path.rglob("*"):
            if _is_link(child) or not (child.is_dir() or child.is_file()):
                raise ApplyError(f"unsafe content in backup: {path.name}")
        existing_backups.append(path)
    backup = backup_parent / (
        f"backup-{time.time_ns():020d}-{uuid.uuid4().hex[:8]}"
    )
    backup.mkdir(mode=0o700)
    try:
        for relative in sorted(_control_files(target)):
            source = _local_path(target, relative)
            destination = _local_path(backup, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    except Exception:
        shutil.rmtree(backup, ignore_errors=True)
        raise
    backups = sorted((*existing_backups, backup))
    for obsolete in backups[:-3]:
        shutil.rmtree(obsolete)
    return backup


def _restore_controls(backup: Path, target: Path) -> None:
    _replace_controls(backup, target)


def _recover_previous_runtime(
    runner: CommandRunner,
    target: Path,
    backup: Path | None,
    ownership: OwnershipSetter,
    timeout: float,
) -> None:
    if backup is not None:
        _restore_controls(backup, target)
    previous = _load_manifest(target)
    _apply_ownership(target, previous, ownership)
    _run(runner, target, previous, ("up", "-d", "--build"), timeout)


def _remove_staging(staging: Path) -> None:
    if staging.exists():
        shutil.rmtree(staging)


def apply_staging(
    layout: RuntimeLayout,
    target_root: Path,
    runner: CommandRunner,
    verifier: Verifier | None = None,
    *,
    ownership: OwnershipSetter | None = None,
    command_timeout: float = 120.0,
) -> ApplyResult:
    """Validate and apply one staging tree without exposing command output."""

    if command_timeout <= 0:
        raise ApplyError("command_timeout must be greater than zero")
    raw_staging = layout.root.expanduser().absolute()
    raw_target = target_root.expanduser().absolute()
    if _is_link(raw_staging) or _is_link(raw_target):
        raise ApplyError("staging or target_root is an unsafe symlink")
    staging = raw_staging.resolve()
    target = raw_target.resolve()
    if not staging.is_dir():
        raise ApplyError("staging runtime does not exist")
    if target == Path(target.anchor) or target == staging:
        raise ApplyError("unsafe target_root")
    if target in staging.parents or staging in target.parents:
        raise ApplyError("staging and target roots must not contain one another")
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.parent / f".{target.name}.apply.lock"
    ownership_setter = ownership or _default_ownership

    with _instance_lock(lock_path):
        validated_root, manifest = _validate_staging(
            layout,
            runner,
            command_timeout,
        )
        if validated_root != staging:
            raise ApplyError("staging root changed during apply")

        if target.exists():
            if not target.is_dir():
                raise ApplyError("existing target_root is not a directory")
            _control_files(target)
            previous = _load_manifest(target)
            if previous.instance_id != manifest.instance_id:
                raise ApplyError("staging instance does not match target runtime")
            changed = not _controls_equal(staging, target)
            backup: Path | None = None
            if changed:
                try:
                    backup = _create_backup(target)
                except ApplyError:
                    raise
                except Exception as error:
                    raise ApplyError(
                        "previous controls could not be backed up"
                    ) from error
                try:
                    _replace_controls(staging, target)
                    current = _load_manifest(target)
                except Exception as error:
                    if backup is not None:
                        try:
                            _restore_controls(backup, target)
                        except Exception:
                            raise ApplyError(
                                "control switch failed and previous controls "
                                "could not be restored"
                            ) from error
                    raise ApplyError(
                        "control switch failed; previous controls restored"
                    ) from error
            else:
                current = previous
            try:
                _apply_ownership(target, current, ownership_setter)
                _run(
                    runner,
                    target,
                    current,
                    ("up", "-d", "--build"),
                    command_timeout,
                )
                verified = _verify(verifier, target)
            except Exception as error:
                try:
                    _recover_previous_runtime(
                        runner,
                        target,
                        backup,
                        ownership_setter,
                        command_timeout,
                    )
                except Exception:
                    raise ApplyError(
                        "apply failed; previous controls were restored but "
                        "the previous project could not be started"
                    ) from error
                raise ApplyError(
                    "apply failed; previous controls and project were restored"
                ) from error
            _remove_staging(staging)
            return ApplyResult(
                target_root=target,
                project_name=current.project_name,
                installed=False,
                changed=changed,
                verified=verified,
                backup_root=backup,
            )

        try:
            staging.rename(target)
        except OSError as error:
            raise ApplyError("cannot atomically install the staging runtime") from error
        try:
            _apply_ownership(target, manifest, ownership_setter)
            _run(
                runner,
                target,
                manifest,
                ("up", "-d", "--build"),
                command_timeout,
            )
            verified = _verify(verifier, target)
        except Exception as error:
            try:
                _run(runner, target, manifest, ("stop",), command_timeout)
            except Exception:
                raise ApplyError(
                    "first install failed and the project could not be stopped; "
                    "diagnostic runtime was preserved"
                ) from error
            raise ApplyError(
                "first install failed; project stopped and diagnostic runtime preserved"
            ) from error
        return ApplyResult(
            target_root=target,
            project_name=manifest.project_name,
            installed=True,
            changed=True,
            verified=verified,
        )
