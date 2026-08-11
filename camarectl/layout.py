"""Filesystem layout for isolated rendered camare deployments."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile


@dataclass(frozen=True)
class RuntimeLayout:
    """Paths belonging to one independently generated staging directory."""

    root: Path
    base_compose: Path
    intel_compose: Path | None
    env: Path
    frigate_config: Path
    caddyfile: Path
    secrets: Path
    portal_data: Path
    manifest: Path

    @property
    def compose_files(self) -> tuple[Path, ...]:
        if self.intel_compose is None:
            return (self.base_compose,)
        return (self.base_compose, self.intel_compose)


def create_staging_layout(
    parent: Path,
    instance_id: str,
    *,
    include_intel: bool,
) -> RuntimeLayout:
    """Create a unique staging tree; an existing runtime is never replaced."""

    staging_parent = parent.expanduser().resolve()
    staging_parent.mkdir(parents=True, exist_ok=True)
    root = Path(
        tempfile.mkdtemp(prefix=f"{instance_id}.staging-", dir=staging_parent)
    )
    frigate = root / "frigate"
    caddy = root / "caddy"
    secrets = root / "secrets"
    portal_data = root / "portal-data"
    for directory, mode in (
        (frigate, 0o750),
        (caddy, 0o750),
        (secrets, 0o700),
        (portal_data, 0o750),
    ):
        directory.mkdir(mode=mode)
        directory.chmod(mode)
    return RuntimeLayout(
        root=root,
        base_compose=root / "compose.yaml",
        intel_compose=(root / "compose.intel.yaml") if include_intel else None,
        env=root / ".env",
        frigate_config=frigate / "config.yml",
        caddyfile=caddy / "Caddyfile",
        secrets=secrets,
        portal_data=portal_data,
        manifest=root / "manifest.json",
    )
