"""Shared least-privilege ownership policy for generated runtime files."""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Mapping


class OwnershipPolicyError(ValueError):
    """A runtime manifest has an unsafe or unsupported ownership policy."""


@dataclass(frozen=True)
class OwnershipRequirement:
    path: PurePosixPath
    uid: int
    gid: int
    mode: int


@dataclass(frozen=True)
class OwnershipPolicy:
    requirements: tuple[OwnershipRequirement, ...]
    legacy: bool = False


OwnershipSetter = Callable[[Path, int, int, int], None]


_PROVIDERS = frozenset({"lan", "cloudflare", "tailscale"})
_CAMERA_SECRET = re.compile(r"^secrets/camera-password-([1-2])$")
_COMMON_POLICY = {
    "portal-data": (65532, 65532, 0o750),
    "secrets": (0, 0, 0o700),
    "secrets/lan-access-key": (0, 65532, 0o440),
}


def _relative_path(value: object) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or ":" in value
        or "\x00" in value
    ):
        raise OwnershipPolicyError("ownership path must be portable and relative")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise OwnershipPolicyError("ownership path must stay inside the runtime root")
    return path


def _is_unsafe_path(path: Path) -> bool:
    try:
        status = path.lstat()
    except OSError:
        return True
    is_reparse = bool(
        getattr(status, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )
    return (
        path.is_symlink()
        or stat.S_ISLNK(status.st_mode)
        or is_reparse
        or not (stat.S_ISREG(status.st_mode) or stat.S_ISDIR(status.st_mode))
    )


def replay_required_ownership(
    root: Path,
    requirements: Iterable[OwnershipRequirement],
    setter: OwnershipSetter,
) -> None:
    """Reapply validated ownership requirements inside one runtime root."""

    for requirement in requirements:
        relative = _relative_path(requirement.path.as_posix())
        if relative != requirement.path:
            raise OwnershipPolicyError("ownership path is not canonical")
        path = root.joinpath(*relative.parts)
        if _is_unsafe_path(path):
            raise OwnershipPolicyError(
                f"ownership target is missing or unsafe: {relative}"
            )
        setter(path, requirement.uid, requirement.gid, requirement.mode)


def parse_required_ownership(
    document: Mapping[str, object] | object,
    *,
    allow_legacy: bool = False,
) -> OwnershipPolicy:
    """Parse and enforce the complete ownership contract for one runtime."""

    if not isinstance(document, Mapping):
        raise OwnershipPolicyError("runtime manifest must be an object")
    provider = document.get("provider")
    if not isinstance(provider, str) or provider not in _PROVIDERS:
        raise OwnershipPolicyError("runtime manifest has an invalid provider")
    raw_policy = document.get("required_ownership")
    if raw_policy is None:
        if not allow_legacy:
            raise OwnershipPolicyError(
                "legacy runtime has no complete required_ownership policy"
            )
        return OwnershipPolicy((), legacy=True)
    if not isinstance(raw_policy, Mapping):
        raise OwnershipPolicyError("required_ownership must be an object")

    is_legacy = "secrets" not in raw_policy
    if is_legacy and not allow_legacy:
        raise OwnershipPolicyError(
            "legacy runtime has no complete required_ownership policy"
        )

    camera_indices: list[int] = []
    for raw_path in raw_policy:
        path = _relative_path(raw_path).as_posix()
        match = _CAMERA_SECRET.fullmatch(path)
        if match is not None:
            camera_indices.append(int(match.group(1)))
    camera_indices.sort()
    if not is_legacy and camera_indices not in ([1], [1, 2]):
        raise OwnershipPolicyError(
            "required_ownership must list one or two sequential camera secrets"
        )

    expected = dict(_COMMON_POLICY)
    for index in camera_indices:
        expected[f"secrets/camera-password-{index}"] = (0, 0, 0o600)
    if provider == "cloudflare":
        expected["secrets/cloudflare-token"] = (65532, 65532, 0o400)
    if is_legacy:
        unexpected = set(raw_policy).difference(expected)
        if unexpected:
            raise OwnershipPolicyError(
                "legacy required_ownership has unexpected paths"
            )
    elif set(raw_policy) != set(expected):
        raise OwnershipPolicyError("required_ownership has unexpected paths")

    requirements: list[OwnershipRequirement] = []
    paths = sorted(raw_policy) if is_legacy else sorted(expected)
    for raw_path in paths:
        raw_requirement = raw_policy.get(raw_path)
        if not isinstance(raw_requirement, Mapping) or set(raw_requirement) != {
            "uid",
            "gid",
            "mode",
        }:
            raise OwnershipPolicyError(
                f"invalid ownership requirement: {raw_path}"
            )
        uid = raw_requirement.get("uid")
        gid = raw_requirement.get("gid")
        raw_mode = raw_requirement.get("mode")
        if (
            not isinstance(uid, int)
            or isinstance(uid, bool)
            or not isinstance(gid, int)
            or isinstance(gid, bool)
            or not isinstance(raw_mode, str)
            or re.fullmatch(r"0[0-7]{3}", raw_mode) is None
        ):
            raise OwnershipPolicyError(
                f"invalid ownership requirement: {raw_path}"
            )
        mode = int(raw_mode, 8)
        if (uid, gid, mode) != expected[raw_path]:
            raise OwnershipPolicyError(
                f"ownership requirement violates runtime policy: {raw_path}"
            )
        requirements.append(
            OwnershipRequirement(PurePosixPath(raw_path), uid, gid, mode)
        )
    return OwnershipPolicy(tuple(requirements), legacy=is_legacy)
