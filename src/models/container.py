"""Container (LXC) models."""

from typing import Any

from pydantic import BaseModel


class ContainerConfig(BaseModel):
    """Container configuration details."""

    vmid: int
    name: str | None = None
    node: str | None = None
    status: str | None = None
    cpus: int | None = None
    maxcpu: int | None = None
    mem: int | None = None
    maxmem: int | None = None
    disk: int | None = None
    maxdisk: int | None = None
    uptime: int | None = None
    cpu: float | None = None
    netin: int | None = None
    netout: int | None = None
    diskread: int | None = None
    diskwrite: int | None = None
    template: bool = False
    tags: str | None = None
    lock: str | None = None


class ContainerStatus(BaseModel):
    """Detailed container status information."""

    status: str
    vmid: int
    name: str | None = None
    cpus: int | None = None
    maxmem: int | None = None
    mem: int | None = None
    maxdisk: int | None = None
    disk: int | None = None
    uptime: int | None = None
    cpu: float | None = None
    netin: int | None = None
    netout: int | None = None
    diskread: int | None = None
    diskwrite: int | None = None
    ha: dict[str, Any] | None = None
    swap: int | None = None
    maxswap: int | None = None


class ContainerSnapshot(BaseModel):
    """Container snapshot information."""

    name: str
    description: str | None = None
    snaptime: int | None = None
    parent: str | None = None
