"""Configuration and lifecycle helpers for camare deployments."""

from .config import ConfigError, load_config
from .commands import main
from .model import (
    AdminConfig,
    DeploymentConfig,
    GroupConfig,
    HardwareConfig,
    LanConfig,
    PublicConfig,
    StreamConfig,
)

__all__ = [
    "ConfigError",
    "AdminConfig",
    "DeploymentConfig",
    "GroupConfig",
    "HardwareConfig",
    "LanConfig",
    "PublicConfig",
    "StreamConfig",
    "load_config",
    "main",
]
