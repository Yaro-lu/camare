"""Safe backup, restore, and uninstall primitives for one camare instance.

The lifecycle layer deliberately treats recordings as external data.  No public
operation in this module accepts a media path, which keeps media outside its
copy and deletion authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Protocol

from .ownership import (
    OwnershipPolicyError,
    OwnershipRequirement,
    OwnershipSetter,
    parse_required_ownership,
    replay_required_ownership,
)


BACKUP_FORMAT_VERSION = 1
RUNTIME_FORMAT_VERSION = 2
SAFE_INSTANCE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SQLITE_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3"})
MUTABLE_CONTROL_FILES = (PurePosixPath("frigate/config.yml"),)
SAFE_ENV_KEYS = frozenset(
    {
        "ADMIN_DISABLED",
        "ADMIN_LOOPBACK_PORT",
        "ADMIN_ORIGIN",
        "ADMIN_TAILSCALE_LOGIN",
        "CAMARE_INSTANCE_ID",
        "CAMARE_MEDIA_PATH",
        "CAMARE_SOURCE_ROOT",
        "CAMERA_GROUPS",
        "CAMERA_PORTAL_IMAGE",
        "CAMERAS",
        "CLOUDFLARED_IMAGE",
        "FRIGATE_ADMIN_BIND_IP",
        "FRIGATE_ADMIN_PORT",
        "FRIGATE_IMAGE",
        "FRIGATE_RENDER_DEVICE",
        "LAN_BIND_IP",
        "LAN_CADDY_IMAGE",
        "LAN_PORT",
        "LIBVA_DRIVER_NAME",
        "LOCAL_BASE_URL",
        "PUBLIC_BASE_URL",
        "PUBLIC_LOOPBACK_PORT",
        "RECORDING_TIMEZONE",
    }
)


class LifecycleError(RuntimeError):
    """A lifecycle operation was rejected or could not finish safely."""


class LifecycleLockedError(LifecycleError):
    """Another apply or lifecycle operation owns the instance lock."""


class CommandRunner(Protocol):
    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        timeout: float,
    ) -> object: ...


Clock = Callable[[], str]
SqliteBackup = Callable[[Path, Path], None]
Replace = Callable[[Path, Path], None]
Verifier = Callable[[Path], bool | None]


@dataclass(frozen=True)
class BackupResult:
    backup_root: Path
    instance_id: str
    entries: tuple[PurePosixPath, ...]


@dataclass(frozen=True)
class RestoreResult:
    backup_root: Path
    target_root: Path
    instance_id: str
    applied: bool
    paths: tuple[PurePosixPath, ...]
    verified: bool = False


@dataclass(frozen=True)
class UninstallResult:
    target_root: Path
    instance_id: str
    project_name: str
    applied: bool
    purge_data: bool
    control_paths: tuple[PurePosixPath, ...]
    data_paths: tuple[PurePosixPath, ...]


@dataclass(frozen=True)
class _BackupEntry:
    artifact: PurePosixPath
    restore_path: PurePosixPath
    kind: str
    sha256: str
    size: int


@dataclass(frozen=True)
class _BackupManifest:
    instance_id: str
    entries: tuple[_BackupEntry, ...]
    required_secret_names: tuple[str, ...]


@dataclass(frozen=True)
class _FileMetadata:
    mode: int
    uid: int | None
    gid: int | None


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse and attributes & reparse)


@contextmanager
def _instance_lock(path: Path):
    """Acquire the same non-blocking lock file used by ``runtime.apply``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if _is_link(path) or (path.exists() and not path.is_file()):
        raise LifecycleError("instance lock path is unsafe")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise LifecycleError("instance lock path could not be opened safely") from error
    handle = os.fdopen(descriptor, "r+b")
    locked = False
    try:
        if _is_link(path):
            raise LifecycleError("instance lock path became unsafe")
        if hasattr(os, "fchmod"):
            os.fchmod(handle.fileno(), 0o600)
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
                raise LifecycleLockedError(
                    "instance is locked by another apply or lifecycle operation"
                ) from error
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise LifecycleLockedError(
                    "instance is locked by another apply or lifecycle operation"
                ) from error
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


def _lock_path(instance_root: Path) -> Path:
    raw = instance_root.expanduser().absolute()
    if _is_link(raw):
        raise LifecycleError("instance root is an unsafe symlink")
    resolved = raw.resolve()
    return resolved.parent / f".{resolved.name}.apply.lock"


@dataclass(frozen=True)
class _RuntimeManifest:
    instance_id: str
    compose_files: tuple[PurePosixPath, ...]
    control_files: tuple[PurePosixPath, ...]
    format_version: int
    required_ownership: tuple[OwnershipRequirement, ...]

    @property
    def project_name(self) -> str:
        return f"camare_{self.instance_id}"


def _default_clock() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _relative(value: object, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise LifecycleError(f"{label} must be a non-empty relative path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or "." in relative.parts
        or "\\" in value
    ):
        raise LifecycleError(f"{label} must stay inside the instance root")
    return relative


def _local(root: Path, relative: PurePosixPath) -> Path:
    return root.joinpath(*relative.parts)


def _allowed_control(relative: PurePosixPath) -> bool:
    if relative == PurePosixPath(".env"):
        return True
    if len(relative.parts) == 1:
        name = relative.name
        return name == "compose.yaml" or (
            name.startswith("compose.") and name.endswith(".yaml")
        )
    return relative in {
        PurePosixPath("frigate/config.yml"),
        PurePosixPath("caddy/Caddyfile"),
    }


def _load_runtime_manifest(
    root: Path,
    *,
    allow_legacy_ownership: bool = False,
) -> _RuntimeManifest:
    if _is_link(root) or not root.is_dir():
        raise LifecycleError("instance root is missing or is an unsafe symlink")
    manifest_path = root / "manifest.json"
    if _is_link(manifest_path) or not manifest_path.is_file():
        raise LifecycleError("runtime manifest is missing or unsafe")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LifecycleError("runtime manifest is invalid") from error
    if not isinstance(document, dict):
        raise LifecycleError("runtime manifest must be an object")
    format_version = document.get("format_version")
    if format_version != RUNTIME_FORMAT_VERSION:
        raise LifecycleError("unsupported runtime manifest version")
    instance_id = document.get("instance_id")
    if not isinstance(instance_id, str) or not SAFE_INSTANCE_ID.fullmatch(instance_id):
        raise LifecycleError("runtime manifest has an unsafe instance_id")
    raw_files = document.get("files")
    if not isinstance(raw_files, dict) or not raw_files:
        raise LifecycleError("runtime manifest has no control file hashes")
    controls: list[PurePosixPath] = []
    for raw_relative, expected in raw_files.items():
        relative = _relative(raw_relative, "control file")
        if not _allowed_control(relative):
            raise LifecycleError(f"runtime manifest lists a non-control path: {relative}")
        path = _local(root, relative)
        if (
            not isinstance(expected, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected)
            or _is_link(path)
            or not path.is_file()
            or _digest(path) != expected
        ):
            raise LifecycleError(f"runtime control hash mismatch: {relative}")
        controls.append(relative)
    raw_compose = document.get("compose_files")
    if not isinstance(raw_compose, list) or not raw_compose:
        raise LifecycleError("runtime manifest has no compose files")
    compose = tuple(_relative(item, "compose file") for item in raw_compose)
    if len(compose) != len(set(compose)):
        raise LifecycleError("runtime manifest has duplicate compose files")
    if any(item not in controls or not _allowed_control(item) for item in compose):
        raise LifecycleError("runtime compose files are not declared controls")
    expected_immutable = {
        PurePosixPath(".env"),
        PurePosixPath("caddy/Caddyfile"),
        *compose,
    }
    if set(controls) != expected_immutable:
        raise LifecycleError("runtime manifest has unexpected immutable controls")
    raw_mutable = document.get("mutable_files")
    if not isinstance(raw_mutable, list):
        raise LifecycleError("runtime manifest has no mutable_files list")
    mutable = tuple(_relative(item, "mutable control") for item in raw_mutable)
    if mutable != MUTABLE_CONTROL_FILES:
        raise LifecycleError("runtime manifest has unexpected mutable controls")
    if set(controls).intersection(mutable):
        raise LifecycleError("runtime control cannot be immutable and mutable")
    for relative in mutable:
        path = _local(root, relative)
        if not _allowed_control(relative) or _is_link(path) or not path.is_file():
            raise LifecycleError(f"runtime mutable control is missing or unsafe: {relative}")
        controls.append(relative)
    try:
        required_ownership = parse_required_ownership(
            document,
            allow_legacy=allow_legacy_ownership,
        ).requirements
    except OwnershipPolicyError as error:
        raise LifecycleError(
            f"runtime ownership policy is invalid: {error}"
        ) from error
    return _RuntimeManifest(
        instance_id=instance_id,
        compose_files=compose,
        control_files=tuple(sorted(controls)),
        format_version=format_version,
        required_ownership=required_ownership,
    )


def _assert_regular(path: Path, label: str) -> None:
    if _is_link(path) or not path.is_file():
        raise LifecycleError(f"{label} is missing or unsafe")


def _assert_nonsecret_env(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise LifecycleError("runtime .env is unreadable") from error
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise LifecycleError("runtime .env contains a malformed line")
        raw_key = stripped.split("=", 1)[0].strip()
        if raw_key != raw_key.upper() or raw_key not in SAFE_ENV_KEYS or raw_key in seen:
            raise LifecycleError(
                "runtime .env contains an undeclared or duplicate key that "
                f"cannot be backed up: {raw_key}"
            )
        seen.add(raw_key)


def _copy_private(source: Path, destination: Path) -> None:
    _assert_regular(source, str(source))
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    destination.chmod(0o600)


def _sqlite_backup(source: Path, destination: Path) -> None:
    _assert_regular(source, str(source))
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    source_uri = source.resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(source_uri, uri=True, timeout=5.0)) as origin:
            with closing(sqlite3.connect(destination)) as copy:
                origin.backup(copy)
                copy.commit()
                result = copy.execute("pragma integrity_check").fetchone()
                if result != ("ok",):
                    raise LifecycleError(f"SQLite backup failed integrity check: {source.name}")
    except sqlite3.Error as error:
        raise LifecycleError(f"cannot back up SQLite database: {source.name}") from error
    destination.chmod(0o600)


def _validate_sqlite_snapshot(path: Path) -> None:
    _assert_regular(path, f"SQLite snapshot {path.name}")
    source_uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(source_uri, uri=True, timeout=5.0)) as connection:
            result = connection.execute("pragma integrity_check").fetchone()
    except sqlite3.Error as error:
        raise LifecycleError(f"invalid SQLite snapshot: {path.name}") from error
    if result != ("ok",):
        raise LifecycleError(f"invalid SQLite snapshot: {path.name}")


def _database_paths(root: Path) -> tuple[Path, ...]:
    directory = root / "frigate"
    if not directory.exists():
        return ()
    if _is_link(directory) or not directory.is_dir():
        raise LifecycleError("Frigate data directory is unsafe")
    databases: list[Path] = []
    for path in directory.iterdir():
        if path.name == "config.yml":
            continue
        if path.suffix.lower() in SQLITE_SUFFIXES:
            _assert_regular(path, f"Frigate database {path.name}")
            databases.append(path)
    return tuple(sorted(databases))


def _required_secret_names(root: Path) -> tuple[str, ...]:
    directory = root / "secrets"
    if not directory.exists() and not _is_link(directory):
        return ()
    if _is_link(directory) or not directory.is_dir():
        raise LifecycleError("secrets directory is unsafe")
    names: list[str] = []
    for path in directory.iterdir():
        if (
            _is_link(path)
            or not path.is_file()
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", path.name)
        ):
            raise LifecycleError("secrets directory contains an unsafe entry")
        names.append(path.name)
    return tuple(sorted(names))


def _validate_required_secrets(root: Path, names: tuple[str, ...]) -> None:
    directory = root / "secrets"
    if names and (_is_link(directory) or not directory.is_dir()):
        raise LifecycleError("required secrets directory is missing or unsafe")
    for name in names:
        path = directory / name
        if _is_link(path) or not path.is_file():
            raise LifecycleError(f"required secret is missing or unsafe: {name}")


def _entry(
    artifact: PurePosixPath,
    restore_path: PurePosixPath,
    kind: str,
    source: Path,
) -> tuple[str, dict[str, object]]:
    return (
        artifact.as_posix(),
        {
            "kind": kind,
            "restore_path": restore_path.as_posix(),
            "sha256": _digest(source),
            "size": source.stat().st_size,
        },
    )


def _write_json_private(path: Path, document: dict[str, object]) -> None:
    data = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree_directories(root: Path) -> None:
    directories = [path for path in root.rglob("*") if path.is_dir()]
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        _fsync_directory(directory)
    _fsync_directory(root)


def _prepare_destination(instance: Path, destination: Path) -> tuple[Path, Path]:
    raw_destination = destination.expanduser().absolute()
    if _is_link(raw_destination) or raw_destination.exists():
        raise LifecycleError("backup destination already exists or is unsafe")
    parent = raw_destination.parent
    resolved_parent = parent.resolve(strict=False)
    final = resolved_parent / raw_destination.name
    if final == instance or final in instance.parents or instance in final.parents:
        raise LifecycleError("backup destination must be outside the instance root")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved_parent = parent.resolve()
    final = resolved_parent / raw_destination.name
    if final == instance or final in instance.parents or instance in final.parents:
        raise LifecycleError("backup destination must be outside the instance root")
    staging = Path(
        tempfile.mkdtemp(prefix=f".{final.name}.partial-", dir=resolved_parent)
    )
    staging.chmod(0o700)
    return staging, final


def _backup_instance_unlocked(
    instance_root: Path,
    destination: Path,
    *,
    clock: Clock = _default_clock,
    sqlite_backup: SqliteBackup = _sqlite_backup,
    replace: Replace = os.replace,
) -> BackupResult:
    """Create an atomic private backup without ever reading recording media."""

    raw_instance = instance_root.expanduser().absolute()
    if _is_link(raw_instance):
        raise LifecycleError("instance root is an unsafe symlink")
    instance = raw_instance.resolve()
    manifest = _load_runtime_manifest(instance, allow_legacy_ownership=True)
    if PurePosixPath(".env") in manifest.control_files:
        _assert_nonsecret_env(instance / ".env")
    required_secret_names = _required_secret_names(instance)
    staging, final = _prepare_destination(instance, destination)
    entries: dict[str, dict[str, object]] = {}
    try:
        for relative in manifest.control_files:
            artifact = PurePosixPath("payload/control") / relative
            output = _local(staging, artifact)
            _copy_private(_local(instance, relative), output)
            key, value = _entry(artifact, relative, "control", output)
            entries[key] = value

        runtime_artifact = PurePosixPath("payload/control/manifest.json")
        runtime_output = _local(staging, runtime_artifact)
        _copy_private(instance / "manifest.json", runtime_output)
        key, value = _entry(
            runtime_artifact,
            PurePosixPath("manifest.json"),
            "control",
            runtime_output,
        )
        entries[key] = value

        portal_state = instance / "portal-data" / "state.json"
        if portal_state.exists() or _is_link(portal_state):
            artifact = PurePosixPath("payload/portal/state.json")
            output = _local(staging, artifact)
            _copy_private(portal_state, output)
            key, value = _entry(
                artifact,
                PurePosixPath("portal-data/state.json"),
                "portal-state",
                output,
            )
            entries[key] = value

        for database in _database_paths(instance):
            artifact = PurePosixPath("payload/frigate") / database.name
            output = _local(staging, artifact)
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            sqlite_backup(database, output)
            _assert_regular(output, f"SQLite backup {database.name}")
            output.chmod(0o600)
            key, value = _entry(
                artifact,
                PurePosixPath("frigate") / database.name,
                "sqlite",
                output,
            )
            entries[key] = value

        created_at = clock()
        if (
            not isinstance(created_at, str)
            or not created_at
            or len(created_at) > 128
            or any(character in created_at for character in "\x00\r\n")
        ):
            raise LifecycleError("backup clock returned an invalid timestamp")
        backup_manifest: dict[str, object] = {
            "format_version": BACKUP_FORMAT_VERSION,
            "kind": "camare-lifecycle-backup",
            "instance_id": manifest.instance_id,
            "source_manifest_format_version": manifest.format_version,
            "created_at": created_at,
            "required_secret_names": list(required_secret_names),
            "entries": dict(sorted(entries.items())),
        }
        manifest_path = staging / "backup-manifest.json"
        _write_json_private(manifest_path, backup_manifest)
        sidecar = staging / "backup-manifest.sha256"
        sidecar_content = f"{_digest(manifest_path)}  backup-manifest.json\n"
        with sidecar.open("xb") as handle:
            handle.write(sidecar_content.encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
        sidecar.chmod(0o600)
        _fsync_tree_directories(staging)
        replace(staging, final)
        _fsync_directory(final.parent)
        return BackupResult(
            backup_root=final,
            instance_id=manifest.instance_id,
            entries=tuple(_relative(item, "backup artifact") for item in sorted(entries)),
        )
    except Exception as error:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(error, LifecycleError):
            raise
        raise LifecycleError("backup failed; partial output was removed") from error


def backup_instance(
    instance_root: Path,
    destination: Path,
    *,
    clock: Clock = _default_clock,
    sqlite_backup: SqliteBackup = _sqlite_backup,
    replace: Replace = os.replace,
) -> BackupResult:
    """Create an atomic private backup while holding the shared apply lock."""

    with _instance_lock(_lock_path(instance_root)):
        return _backup_instance_unlocked(
            instance_root,
            destination,
            clock=clock,
            sqlite_backup=sqlite_backup,
            replace=replace,
        )


def _expected_artifact(kind: str, restore_path: PurePosixPath) -> PurePosixPath:
    if kind == "control" and (
        restore_path == PurePosixPath("manifest.json")
        or _allowed_control(restore_path)
    ):
        return PurePosixPath("payload/control") / restore_path
    if kind == "portal-state" and restore_path == PurePosixPath(
        "portal-data/state.json"
    ):
        return PurePosixPath("payload/portal/state.json")
    if (
        kind == "sqlite"
        and len(restore_path.parts) == 2
        and restore_path.parts[0] == "frigate"
        and restore_path.suffix.lower() in SQLITE_SUFFIXES
    ):
        return PurePosixPath("payload/frigate") / restore_path.name
    raise LifecycleError(f"backup contains an unauthorized restore path: {restore_path}")


def _load_backup(root: Path) -> _BackupManifest:
    if _is_link(root) or not root.is_dir():
        raise LifecycleError("backup root is missing or is an unsafe symlink")
    manifest_path = root / "backup-manifest.json"
    sidecar_path = root / "backup-manifest.sha256"
    _assert_regular(manifest_path, "backup manifest")
    _assert_regular(sidecar_path, "backup manifest checksum")
    expected_sidecar = f"{_digest(manifest_path)}  backup-manifest.json\n"
    try:
        sidecar = sidecar_path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as error:
        raise LifecycleError("backup manifest checksum is invalid") from error
    if sidecar != expected_sidecar:
        raise LifecycleError("backup manifest checksum mismatch")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LifecycleError("backup manifest is invalid") from error
    if not isinstance(document, dict):
        raise LifecycleError("backup manifest must be an object")
    if (
        document.get("format_version") != BACKUP_FORMAT_VERSION
        or document.get("kind") != "camare-lifecycle-backup"
        or document.get("source_manifest_format_version")
        != RUNTIME_FORMAT_VERSION
    ):
        raise LifecycleError("unsupported backup format or source version")
    instance_id = document.get("instance_id")
    if not isinstance(instance_id, str) or not SAFE_INSTANCE_ID.fullmatch(instance_id):
        raise LifecycleError("backup has an unsafe instance_id")
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, dict) or not raw_entries:
        raise LifecycleError("backup has no entries")
    raw_secret_names = document.get("required_secret_names")
    if not isinstance(raw_secret_names, list):
        raise LifecycleError("backup has no required_secret_names list")
    required_secret_names: list[str] = []
    for name in raw_secret_names:
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)
            or name in required_secret_names
        ):
            raise LifecycleError("backup has an unsafe required secret name")
        required_secret_names.append(name)
    if required_secret_names != sorted(required_secret_names):
        raise LifecycleError("backup required secret names must be sorted")
    entries: list[_BackupEntry] = []
    restore_paths: set[PurePosixPath] = set()
    for raw_artifact, raw_entry in raw_entries.items():
        artifact = _relative(raw_artifact, "backup artifact")
        if not isinstance(raw_entry, dict):
            raise LifecycleError(f"invalid backup entry: {artifact}")
        restore_path = _relative(raw_entry.get("restore_path"), "restore path")
        kind = raw_entry.get("kind")
        expected_hash = raw_entry.get("sha256")
        size = raw_entry.get("size")
        if not isinstance(kind, str) or artifact != _expected_artifact(
            kind, restore_path
        ):
            raise LifecycleError(f"backup artifact path mismatch: {artifact}")
        if restore_path in restore_paths:
            raise LifecycleError(f"duplicate backup restore path: {restore_path}")
        source = _local(root, artifact)
        if (
            not isinstance(expected_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or _is_link(source)
            or not source.is_file()
            or source.stat().st_size != size
            or _digest(source) != expected_hash
        ):
            raise LifecycleError(f"backup entry hash or size mismatch: {artifact}")
        if kind == "sqlite":
            _validate_sqlite_snapshot(source)
        if kind == "control" and restore_path == PurePosixPath(".env"):
            _assert_nonsecret_env(source)
        restore_paths.add(restore_path)
        entries.append(
            _BackupEntry(artifact, restore_path, kind, expected_hash, size)
        )
    if PurePosixPath("manifest.json") not in restore_paths:
        raise LifecycleError("backup does not contain its runtime manifest")
    runtime_copy = root / "payload" / "control" / "manifest.json"
    try:
        runtime_document = json.loads(runtime_copy.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LifecycleError("backed-up runtime manifest is invalid") from error
    if (
        not isinstance(runtime_document, dict)
        or runtime_document.get("format_version") != RUNTIME_FORMAT_VERSION
        or runtime_document.get("instance_id") != instance_id
    ):
        raise LifecycleError("backed-up runtime manifest does not match the backup")
    raw_runtime_files = runtime_document.get("files")
    if not isinstance(raw_runtime_files, dict) or not raw_runtime_files:
        raise LifecycleError("backed-up runtime manifest has no control hashes")
    by_restore_path = {entry.restore_path: entry for entry in entries}
    runtime_control_paths: set[PurePosixPath] = set()
    for raw_relative, expected_hash in raw_runtime_files.items():
        relative = _relative(raw_relative, "backed-up control")
        entry = by_restore_path.get(relative)
        if (
            not _allowed_control(relative)
            or not isinstance(expected_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
            or entry is None
            or entry.kind != "control"
            or entry.sha256 != expected_hash
        ):
            raise LifecycleError(f"backed-up control manifest mismatch: {relative}")
        runtime_control_paths.add(relative)
    raw_mutable = runtime_document.get("mutable_files")
    if not isinstance(raw_mutable, list):
        raise LifecycleError("backed-up runtime manifest has no mutable_files list")
    mutable_paths = tuple(
        _relative(item, "backed-up mutable control") for item in raw_mutable
    )
    if mutable_paths != MUTABLE_CONTROL_FILES:
        raise LifecycleError("backed-up runtime manifest has unexpected mutable controls")
    for relative in mutable_paths:
        entry = by_restore_path.get(relative)
        if (
            relative in runtime_control_paths
            or entry is None
            or entry.kind != "control"
        ):
            raise LifecycleError(
                f"backed-up mutable control manifest mismatch: {relative}"
            )
    backed_control_paths = {
        entry.restore_path for entry in entries if entry.kind == "control"
    }
    expected_backed_controls = runtime_control_paths.union(
        mutable_paths,
        {PurePosixPath("manifest.json")},
    )
    if backed_control_paths != expected_backed_controls:
        raise LifecycleError("backup contains undeclared control files")
    raw_compose = runtime_document.get("compose_files")
    if not isinstance(raw_compose, list) or not raw_compose:
        raise LifecycleError("backed-up runtime manifest has no compose files")
    compose = tuple(_relative(item, "backed-up compose file") for item in raw_compose)
    if (
        len(compose) != len(set(compose))
        or any(item not in runtime_control_paths for item in compose)
    ):
        raise LifecycleError("backed-up runtime compose files are invalid")
    expected_immutable = {
        PurePosixPath(".env"),
        PurePosixPath("caddy/Caddyfile"),
        *compose,
    }
    if runtime_control_paths != expected_immutable:
        raise LifecycleError("backed-up runtime has unexpected immutable controls")
    return _BackupManifest(
        instance_id,
        tuple(sorted(entries, key=lambda e: e.restore_path)),
        tuple(required_secret_names),
    )


def _compose_arguments(
    root: Path,
    manifest: _RuntimeManifest,
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
        arguments.extend(("-f", str(_local(root, relative))))
    arguments.extend(action)
    return tuple(arguments)


def _run_compose(
    runner: CommandRunner,
    root: Path,
    manifest: _RuntimeManifest,
    action: tuple[str, ...],
    timeout: float,
) -> object:
    arguments = _compose_arguments(root, manifest, action)
    forbidden = frozenset({"-v", "--volumes", "prune"})
    if forbidden.intersection(arguments):
        raise LifecycleError("unsafe Docker lifecycle command was rejected")
    return runner(arguments, cwd=root, timeout=timeout)


def _require_compose_wait(
    runner: CommandRunner,
    root: Path,
    timeout: float,
) -> None:
    """Fail closed before mutation unless Compose advertises standalone --wait."""

    try:
        result = runner(
            ("docker", "compose", "up", "--help"),
            cwd=root,
            timeout=timeout,
        )
    except Exception as error:
        raise LifecycleError(
            "Docker Compose --wait capability could not be verified"
        ) from error
    returncode = getattr(result, "returncode", None)
    stdout = getattr(result, "stdout", "")
    stderr = getattr(result, "stderr", "")
    help_text = "\n".join(
        value for value in (stdout, stderr) if isinstance(value, str)
    )
    if returncode != 0 or re.search(r"(?<![\w-])--wait(?![\w-])", help_text) is None:
        raise LifecycleError(
            "Docker Compose must support the standalone --wait option"
        )


def _default_ownership(path: Path, uid: int, gid: int, mode: int) -> None:
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise LifecycleError("replaying runtime ownership requires Linux root")
    os.chown(path, uid, gid, follow_symlinks=False)
    path.chmod(mode, follow_symlinks=False)


def _replay_manifest_ownership(
    root: Path,
    manifest: _RuntimeManifest,
    ownership: OwnershipSetter,
) -> None:
    try:
        replay_required_ownership(root, manifest.required_ownership, ownership)
    except (OSError, OwnershipPolicyError) as error:
        raise LifecycleError("runtime ownership could not be replayed safely") from error


def _safe_restore_target(root: Path, relative: PurePosixPath) -> Path:
    current = root
    for part in relative.parts:
        current = current / part
        if _is_link(current):
            raise LifecycleError(f"restore target contains a symlink: {relative}")
    return current


def _trusted_new_file_mode(relative: PurePosixPath) -> int:
    if relative == PurePosixPath("caddy/Caddyfile"):
        return 0o644
    if relative == PurePosixPath(".env"):
        return 0o600
    if relative == PurePosixPath("manifest.json"):
        return 0o640
    if relative.name.startswith("compose") and relative.suffix == ".yaml":
        return 0o640
    if relative == PurePosixPath("frigate/config.yml"):
        return 0o640
    if relative == PurePosixPath("portal-data/state.json"):
        return 0o600
    if relative.parts and relative.parts[0] == "frigate":
        return 0o600
    raise LifecycleError(f"no trusted restore mode for: {relative}")


def _metadata_for_existing(path: Path) -> _FileMetadata:
    _assert_regular(path, str(path))
    status = path.stat(follow_symlinks=False)
    return _FileMetadata(
        mode=stat.S_IMODE(status.st_mode),
        uid=getattr(status, "st_uid", None),
        gid=getattr(status, "st_gid", None),
    )


def _metadata_for_missing(
    destination: Path,
    relative: PurePosixPath,
) -> _FileMetadata:
    parent = destination.parent
    if _is_link(parent) or not parent.is_dir():
        raise LifecycleError(f"restore parent is missing or unsafe: {relative.parent}")
    status = parent.stat(follow_symlinks=False)
    return _FileMetadata(
        mode=_trusted_new_file_mode(relative),
        uid=getattr(status, "st_uid", None),
        gid=getattr(status, "st_gid", None),
    )


def _atomic_copy(
    source: Path,
    destination: Path,
    replace: Replace,
    metadata: _FileMetadata,
) -> None:
    _assert_regular(source, str(source))
    if _is_link(destination.parent) or not destination.parent.is_dir():
        raise LifecycleError("restore destination parent is missing or unsafe")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.restore-",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as reader, temporary.open("wb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            if (
                os.name == "posix"
                and metadata.uid is not None
                and metadata.gid is not None
                and hasattr(os, "fchown")
            ):
                os.fchown(writer.fileno(), metadata.uid, metadata.gid)
            if hasattr(os, "fchmod"):
                os.fchmod(writer.fileno(), metadata.mode)
            os.fsync(writer.fileno())
        replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _sqlite_sidecars(relative: PurePosixPath) -> tuple[PurePosixPath, ...]:
    return (
        PurePosixPath(relative.as_posix() + "-wal"),
        PurePosixPath(relative.as_posix() + "-shm"),
    )


def _stage_restore(
    backup: Path,
    target: Path,
    archive: _BackupManifest,
) -> Path:
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.restore-stage-", dir=target.parent)
    )
    staging.chmod(0o700)
    try:
        for entry in archive.entries:
            destination = _local(staging, entry.restore_path)
            _copy_private(_local(backup, entry.artifact), destination)
            if _digest(destination) != entry.sha256:
                raise LifecycleError(
                    f"restore staging hash mismatch: {entry.restore_path}"
                )
        return staging
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _snapshot_restore_targets(
    target: Path,
    entries: tuple[_BackupEntry, ...],
) -> tuple[
    Path,
    tuple[PurePosixPath, ...],
    tuple[PurePosixPath, ...],
    dict[PurePosixPath, _FileMetadata],
]:
    rollback = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.restore-rollback-", dir=target.parent)
    )
    rollback.chmod(0o700)
    affected: set[PurePosixPath] = {entry.restore_path for entry in entries}
    for entry in entries:
        if entry.kind == "sqlite":
            affected.update(_sqlite_sidecars(entry.restore_path))
    missing: list[PurePosixPath] = []
    metadata: dict[PurePosixPath, _FileMetadata] = {}
    try:
        for relative in sorted(affected):
            source = _safe_restore_target(target, relative)
            if source.exists() or _is_link(source):
                metadata[relative] = _metadata_for_existing(source)
                _copy_private(source, _local(rollback, relative))
            else:
                missing.append(relative)
        return rollback, tuple(sorted(affected)), tuple(missing), metadata
    except Exception:
        shutil.rmtree(rollback, ignore_errors=True)
        raise


def _roll_back_restore(
    rollback: Path,
    target: Path,
    affected: tuple[PurePosixPath, ...],
    missing: tuple[PurePosixPath, ...],
    metadata: dict[PurePosixPath, _FileMetadata],
    replace: Replace,
) -> None:
    absent = frozenset(missing)
    for relative in affected:
        destination = _safe_restore_target(target, relative)
        source = _local(rollback, relative)
        if relative in absent:
            if _is_link(destination) or (
                destination.exists() and not destination.is_file()
            ):
                raise LifecycleError(f"cannot remove unsafe rollback target: {relative}")
            destination.unlink(missing_ok=True)
        else:
            _atomic_copy(source, destination, replace, metadata[relative])


def _restore_instance_unlocked(
    backup_root: Path,
    target_root: Path,
    runner: CommandRunner | None = None,
    *,
    apply: bool = False,
    command_timeout: float = 120.0,
    replace: Replace = os.replace,
    verifier: Verifier | None = None,
    ownership: OwnershipSetter | None = None,
) -> RestoreResult:
    """Validate a backup and, only with ``apply=True``, restore it."""

    if command_timeout <= 0:
        raise LifecycleError("command_timeout must be greater than zero")
    backup = backup_root.expanduser().absolute().resolve()
    raw_target = target_root.expanduser().absolute()
    if _is_link(raw_target):
        raise LifecycleError("restore target is an unsafe symlink")
    target = raw_target.resolve()
    archive = _load_backup(backup)
    current = _load_runtime_manifest(target, allow_legacy_ownership=True)
    if current.instance_id != archive.instance_id:
        raise LifecycleError("backup instance_id does not match the target")
    _validate_required_secrets(target, archive.required_secret_names)
    paths = tuple(entry.restore_path for entry in archive.entries)
    if not apply:
        return RestoreResult(
            backup_root=backup,
            target_root=target,
            instance_id=archive.instance_id,
            applied=False,
            paths=paths,
            verified=False,
        )
    if runner is None:
        raise LifecycleError("restore apply requires a command runner")
    if backup == target or backup in target.parents or target in backup.parents:
        raise LifecycleError("backup and target roots must be separate")

    _require_compose_wait(runner, target, command_timeout)
    ownership_setter = ownership or _default_ownership

    staging = _stage_restore(backup, target, archive)
    rollback: Path | None = None
    affected: tuple[PurePosixPath, ...] = ()
    missing: tuple[PurePosixPath, ...] = ()
    target_metadata: dict[PurePosixPath, _FileMetadata] = {}
    stopped = False
    restored_manifest: _RuntimeManifest | None = None
    restored_start_attempted = False
    preserve_rollback = False
    try:
        try:
            _run_compose(
                runner,
                target,
                current,
                ("stop", "camera-portal", "frigate"),
                command_timeout,
            )
        except BaseException as stop_error:
            try:
                _replay_manifest_ownership(target, current, ownership_setter)
                _run_compose(
                    runner,
                    target,
                    current,
                    ("up", "-d", "--no-build", "--wait", "--remove-orphans"),
                    command_timeout,
                )
            except BaseException as recovery_error:
                raise LifecycleError(
                    "restore writers could not be stopped and the previous "
                    "project could not be reasserted"
                ) from recovery_error
            if not isinstance(stop_error, Exception):
                raise
            raise LifecycleError(
                "restore writers could not be stopped; target was not modified"
            ) from stop_error
        stopped = True
        rollback, affected, missing, target_metadata = _snapshot_restore_targets(
            target, archive.entries
        )
        restored_sqlite: list[PurePosixPath] = []
        for entry in archive.entries:
            destination = _safe_restore_target(target, entry.restore_path)
            metadata = target_metadata.get(entry.restore_path)
            if metadata is None:
                metadata = _metadata_for_missing(destination, entry.restore_path)
            _atomic_copy(
                _local(staging, entry.restore_path),
                destination,
                replace,
                metadata,
            )
            if entry.kind == "sqlite":
                restored_sqlite.append(entry.restore_path)
        for relative in restored_sqlite:
            for sidecar in _sqlite_sidecars(relative):
                path = _safe_restore_target(target, sidecar)
                if _is_link(path) or (path.exists() and not path.is_file()):
                    raise LifecycleError(f"unsafe SQLite sidecar: {sidecar}")
                path.unlink(missing_ok=True)
        restored_manifest = _load_runtime_manifest(
            target,
            allow_legacy_ownership=True,
        )
        if restored_manifest.instance_id != archive.instance_id:
            raise LifecycleError("restored controls changed the instance_id")
        _replay_manifest_ownership(target, restored_manifest, ownership_setter)
        restored_start_attempted = True
        _run_compose(
            runner,
            target,
            restored_manifest,
            ("up", "-d", "--no-build", "--wait", "--remove-orphans"),
            command_timeout,
        )
        verified = False
        if verifier is not None:
            verification = verifier(target)
            if verification is False:
                raise LifecycleError("restored runtime verification failed")
            verified = True
        stopped = False
        return RestoreResult(
            backup_root=backup,
            target_root=target,
            instance_id=archive.instance_id,
            applied=True,
            paths=paths,
            verified=verified,
        )
    except BaseException as error:
        if stopped:
            try:
                if restored_start_attempted:
                    if restored_manifest is None:
                        raise LifecycleError(
                            "restored project state is unknown; rollback was stopped"
                        )
                    _run_compose(
                        runner,
                        target,
                        restored_manifest,
                        ("stop", "camera-portal", "frigate"),
                        command_timeout,
                    )
                if rollback is not None:
                    _roll_back_restore(
                        rollback,
                        target,
                        affected,
                        missing,
                        target_metadata,
                        replace,
                    )
                previous = _load_runtime_manifest(
                    target,
                    allow_legacy_ownership=True,
                )
                _replay_manifest_ownership(target, previous, ownership_setter)
                _run_compose(
                    runner,
                    target,
                    previous,
                    ("up", "-d", "--no-build", "--wait", "--remove-orphans"),
                    command_timeout,
                )
            except BaseException as recovery_error:
                preserve_rollback = rollback is not None
                suffix = (
                    f"; recovery copy preserved at {rollback}"
                    if rollback is not None
                    else ""
                )
                raise LifecycleError(
                    "restore failed and the previous instance could not be recovered"
                    + suffix
                ) from recovery_error
        if not isinstance(error, Exception):
            raise
        if isinstance(error, LifecycleError):
            raise
        raise LifecycleError("restore failed; previous data was restored") from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if rollback is not None and not preserve_rollback:
            shutil.rmtree(rollback, ignore_errors=True)


def restore_instance(
    backup_root: Path,
    target_root: Path,
    runner: CommandRunner | None = None,
    *,
    apply: bool = False,
    command_timeout: float = 120.0,
    replace: Replace = os.replace,
    verifier: Verifier | None = None,
    ownership: OwnershipSetter | None = None,
) -> RestoreResult:
    """Validate or restore an instance while holding the shared apply lock."""

    with _instance_lock(_lock_path(target_root)):
        return _restore_instance_unlocked(
            backup_root,
            target_root,
            runner,
            apply=apply,
            command_timeout=command_timeout,
            replace=replace,
            verifier=verifier,
            ownership=ownership,
        )


def _reject_symlinks_in_tree(path: Path, label: str) -> None:
    if _is_link(path):
        raise LifecycleError(f"{label} is an unsafe link or junction")
    if not path.exists():
        return
    if not path.is_dir():
        raise LifecycleError(f"{label} is not a directory")
    for entry in os.scandir(path):
        candidate = Path(entry.path)
        if _is_link(candidate):
            raise LifecycleError(f"{label} contains an unsafe link or junction")
        if entry.is_dir(follow_symlinks=False):
            _reject_symlinks_in_tree(candidate, label)
        elif not entry.is_file(follow_symlinks=False):
            raise LifecycleError(f"{label} contains a non-regular entry")


def _uninstall_control_paths(
    root: Path, manifest: _RuntimeManifest
) -> tuple[PurePosixPath, ...]:
    paths = set(manifest.control_files)
    paths.add(PurePosixPath("manifest.json"))
    secrets = root / "secrets"
    if secrets.exists() or _is_link(secrets):
        paths.add(PurePosixPath("secrets"))
    old_backups = root / ".camare-backups"
    if old_backups.exists() or _is_link(old_backups):
        paths.add(PurePosixPath(".camare-backups"))
    return tuple(sorted(paths))


def _persistent_data_paths(root: Path) -> tuple[PurePosixPath, ...]:
    paths: set[PurePosixPath] = set()
    portal = root / "portal-data" / "state.json"
    if portal.exists() or _is_link(portal):
        _assert_regular(portal, "Portal state")
        paths.add(PurePosixPath("portal-data/state.json"))
    for database in _database_paths(root):
        relative = PurePosixPath("frigate") / database.name
        paths.add(relative)
        for sidecar in _sqlite_sidecars(relative):
            path = _safe_restore_target(root, sidecar)
            if path.exists() or _is_link(path):
                _assert_regular(path, f"SQLite sidecar {sidecar}")
                paths.add(sidecar)
    return tuple(sorted(paths))


def _uninstall_instance_unlocked(
    target_root: Path,
    runner: CommandRunner | None = None,
    *,
    apply: bool = False,
    purge_data: bool = False,
    command_timeout: float = 120.0,
    replace: Replace = os.replace,
    ownership: OwnershipSetter | None = None,
) -> UninstallResult:
    """Plan or execute an instance-scoped uninstall; media is never in scope."""

    if command_timeout <= 0:
        raise LifecycleError("command_timeout must be greater than zero")
    raw_target = target_root.expanduser().absolute()
    if _is_link(raw_target):
        raise LifecycleError("uninstall target is an unsafe symlink")
    target = raw_target.resolve()
    manifest = _load_runtime_manifest(target, allow_legacy_ownership=True)
    controls = _uninstall_control_paths(target, manifest)
    data = _persistent_data_paths(target)
    if not apply:
        return UninstallResult(
            target_root=target,
            instance_id=manifest.instance_id,
            project_name=manifest.project_name,
            applied=False,
            purge_data=purge_data,
            control_paths=controls,
            data_paths=data,
        )
    if runner is None:
        raise LifecycleError("uninstall apply requires a command runner")

    _require_compose_wait(runner, target, command_timeout)
    ownership_setter = ownership or _default_ownership

    removals = (
        tuple(sorted(set(controls).union(data))) if purge_data else ()
    )
    for relative in removals:
        candidate = _safe_restore_target(target, relative)
        if not candidate.exists():
            raise LifecycleError(f"uninstall path disappeared: {relative}")
        if relative in {
            PurePosixPath("secrets"),
            PurePosixPath(".camare-backups"),
        }:
            _reject_symlinks_in_tree(candidate, f"managed directory {relative}")
        else:
            _assert_regular(candidate, f"uninstall path {relative}")

    try:
        _run_compose(
            runner,
            target,
            manifest,
            ("down", "--remove-orphans"),
            command_timeout,
        )
    except Exception as error:
        raise LifecycleError("instance containers could not be removed") from error

    if not purge_data:
        return UninstallResult(
            target_root=target,
            instance_id=manifest.instance_id,
            project_name=manifest.project_name,
            applied=True,
            purge_data=False,
            control_paths=controls,
            data_paths=data,
        )

    quarantine = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.uninstall-", dir=target.parent)
    )
    quarantine.chmod(0o700)
    moved: list[PurePosixPath] = []
    try:
        for relative in removals:
            source = _safe_restore_target(target, relative)
            destination = _local(quarantine, relative)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            replace(source, destination)
            moved.append(relative)
    except BaseException as error:
        try:
            for relative in reversed(moved):
                source = _local(quarantine, relative)
                destination = _safe_restore_target(target, relative)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                replace(source, destination)
            if any(
                _is_link(path) or path.is_file() for path in quarantine.rglob("*")
            ):
                raise LifecycleError("quarantine still contains unrecovered files")
            _replay_manifest_ownership(target, manifest, ownership_setter)
            _run_compose(
                runner,
                target,
                manifest,
                ("up", "-d", "--no-build", "--wait", "--remove-orphans"),
                command_timeout,
            )
        except BaseException as recovery_error:
            remaining = any(
                _is_link(path) or path.is_file() for path in quarantine.rglob("*")
            )
            if remaining:
                raise LifecycleError(
                    "uninstall failed and the previous instance could not be "
                    f"recovered; recovery copy preserved at {quarantine}"
                ) from recovery_error
            shutil.rmtree(quarantine, ignore_errors=True)
            raise LifecycleError(
                "uninstall failed; previous files were restored but the project "
                "could not be restarted"
            ) from recovery_error
        shutil.rmtree(quarantine, ignore_errors=True)
        if not isinstance(error, Exception):
            raise
        raise LifecycleError("uninstall failed; previous instance was restored") from error

    try:
        shutil.rmtree(quarantine)
    except OSError as error:
        raise LifecycleError(
            f"instance controls were removed but quarantine cleanup failed: {quarantine}"
        ) from error
    for directory in (
        target / "caddy",
        target / "portal-data",
        target / "frigate",
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    return UninstallResult(
        target_root=target,
        instance_id=manifest.instance_id,
        project_name=manifest.project_name,
        applied=True,
        purge_data=purge_data,
        control_paths=controls,
        data_paths=data,
    )


def uninstall_instance(
    target_root: Path,
    runner: CommandRunner | None = None,
    *,
    apply: bool = False,
    purge_data: bool = False,
    command_timeout: float = 120.0,
    replace: Replace = os.replace,
    ownership: OwnershipSetter | None = None,
) -> UninstallResult:
    """Plan or uninstall an instance while holding the shared apply lock."""

    with _instance_lock(_lock_path(target_root)):
        return _uninstall_instance_unlocked(
            target_root,
            runner,
            apply=apply,
            purge_data=purge_data,
            command_timeout=command_timeout,
            replace=replace,
            ownership=ownership,
        )
