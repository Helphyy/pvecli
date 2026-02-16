"""VM (QEMU) models."""

from typing import Any

from pydantic import BaseModel, Field


class VMConfig(BaseModel):
    """VM configuration details."""

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
    qmpstatus: str | None = None
    pid: int | None = None


class VMStatus(BaseModel):
    """Detailed VM status information."""

    status: str
    vmid: int
    name: str | None = None
    qmpstatus: str | None = None
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
    agent: int | None = None
    balloon: int | None = None
    ballooninfo: dict[str, Any] | None = None
    blockstat: dict[str, Any] | None = None
    nics: dict[str, Any] | None = None
    proxmox_support: dict[str, Any] | None = None
    running_machine: str | None = None
    running_qemu: str | None = None


class VMSnapshot(BaseModel):
    """VM snapshot information."""

    name: str
    description: str | None = None
    snaptime: int | None = None
    vmstate: bool | None = None
    parent: str | None = None


class VMCloneOptions(BaseModel):
    """Options for cloning a VM."""

    newid: int = Field(..., description="New VM ID")
    name: str | None = Field(None, description="New VM name")
    target: str | None = Field(None, description="Target node")
    full: bool = Field(False, description="Create a full copy")
    snapname: str | None = Field(None, description="Clone from snapshot")
    pool: str | None = Field(None, description="Add to pool")
    storage: str | None = Field(None, description="Target storage")
    format: str | None = Field(None, description="Target format (qcow2, raw, vmdk)")
    description: str | None = Field(None, description="VM description")


class TaskStatus(BaseModel):
    """Task status information."""

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
