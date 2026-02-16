"""API client and authentication."""

from .auth import AuthHandler
from .client import ProxmoxClient
from .exceptions import (
    APIError,
    AuthenticationError,
    ConfigError,
    NetworkError,
    PermissionError,
    PVECliError,
    ResourceNotFoundError,
    TimeoutError,
)

__all__ = [
    "APIError",
    "AuthHandler",
    "AuthenticationError",
    "ConfigError",
    "NetworkError",
    "PermissionError",
    "ProxmoxClient",
    "PVECliError",
    "ResourceNotFoundError",
    "TimeoutError",
]
