"""Immutable public configuration model for camare."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class PublicConfig:
    origin: str
    loopback_port: int
    cloudflare_token_file: Path | None = field(default=None, repr=False)


@dataclass(frozen=True)
class AdminConfig:
    origin: str
    loopback_port: int
    tailscale_login: str
    frigate_bind_ip: str
    frigate_port: int


@dataclass(frozen=True)
class LanConfig:
    bind_ip: str
    port: int


@dataclass(frozen=True)
class HardwareConfig:
    mode: str
    render_device: PurePosixPath | None
    va_driver: str | None


@dataclass(frozen=True)
class StreamConfig:
    id: str
    name: str
    path: str


@dataclass(frozen=True)
class GroupConfig:
    id: str
    name: str
    host: str
    rtsp_user: str
    rtsp_port: int
    password_file: Path = field(repr=False)
    record_audio: bool = False
    enabled: bool = True
    streams: tuple[StreamConfig, ...] = ()


@dataclass(frozen=True)
class DeploymentConfig:
    instance_id: str
    runtime_dir: PurePosixPath
    timezone: str
    provider: str
    public: PublicConfig
    admin: AdminConfig
    lan: LanConfig
    media: PurePosixPath
    retention: float
    hardware: HardwareConfig
    groups: tuple[GroupConfig, ...]
