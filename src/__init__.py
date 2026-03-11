"""pvecli - Modern CLI for Proxmox VE API."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pvecli")
except PackageNotFoundError:
    __version__ = "unknown"
