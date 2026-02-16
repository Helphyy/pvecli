"""Storage models."""

from typing import Any

from pydantic import BaseModel


class StorageInfo(BaseModel):
    """Storage information."""

    storage: str
    type: str
    content: str | None = None
    shared: bool = False
    active: bool = True
    enabled: bool = True
    total: int | None = None
    used: int | None = None
    avail: int | None = None
    used_fraction: float | None = None


class StorageContent(BaseModel):
    """Storage content item."""

    volid: str
    content: str
    format: str | None = None
    size: int | None = None
    used: int | None = None
    vmid: int | None = None
    ctime: int | None = None


class ClusterResource(BaseModel):
    """Cluster resource information."""

    id: str
    type: str
    status: str | None = None
    node: str | None = None
    vmid: int | None = None
    name: str | None = None
    cpu: float | None = None
    maxcpu: int | None = None
    mem: int | None = None
    maxmem: int | None = None
    disk: int | None = None
    maxdisk: int | None = None
    uptime: int | None = None
    level: str | None = None
    storage: str | None = None
    content: str | None = None
    shared: bool | None = None


class ClusterTask(BaseModel):
    """Cluster task information."""

    model_config = {"extra": "allow"}  # Allow extra fields from API

    upid: str
    node: str
    pid: int
    pstart: int
    starttime: int
    type: str
    status: str
    user: str
    id: str | None = None
    exitstatus: str | None = None
    endtime: int | None = None
