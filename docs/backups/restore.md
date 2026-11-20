---
icon: lucide/archive-restore
title: Restore a backup
description: Restore a backup as a new guest, or over an existing stopped guest, on the node and storage you choose.
---

# Restore a backup

Find the volid of the backup with [backup list](../cli/backup/list.md) (see [Check backup coverage](coverage.md#list-the-backups)), then pass it to [backup restore](../cli/backup/restore.md):

```bash
pvecli backup list --vmid 822 -p prod
```

What pvecli checks before it restores anything:

```mermaid
flowchart TD
  A[backup restore VOLID]:::run --> C{--vmid given<br/>and taken?}:::check
  C -- "no: that ID or the next free one" --> N[New guest: confirm]:::step
  C -- yes --> E{"--force given, same type,<br/>guest stopped, its node<br/>reads the backup?"}:::check
  E -- no --> X[Stop, nothing changed]:::stop
  E -- yes --> F[Overwrite: type the ID again]:::step
  N --> R[Restore, then start with --start]:::done
  F --> R

  classDef check fill:#e8f3fc,stroke:#0078cf,color:#0b3d66
  classDef stop fill:#ffe3e3,stroke:#e03131,color:#7a1010
  classDef run fill:#e57000,stroke:#b35800,color:#ffffff
  classDef step fill:#fff1e3,stroke:#e57000,color:#5c2d00
  classDef done fill:#d3f9d8,stroke:#2f9e44,color:#0f4d1c
```

`-y` skips the confirmation and the typed ID.

## As a new guest

With `--vmid`, the backup becomes a new guest with that ID; without it, pvecli takes the next free ID of the cluster. The original guest is not touched:

--8<-- "screens/backups-restore-new.html"

`--unique` gives the network cards of the new guest new random MAC addresses. Use it whenever the original guest may run at the same time as the copy, or both get the same MAC address on the network.

## Where it is restored

| Target | Default | Change it with |
|---|---|---|
| Node | For a backup on a shared storage (PBS, NFS...), the node of the original guest when it still exists and is online, else the first online node that reads the storage. For a backup on a local storage, the node that holds the file. | `--node` (a shared storage only: a local file can only be restored on its node) |
| Storage of a VM | The storages recorded in the backup, disk by disk. | `-s`, `--storage`: every disk goes there. |
| Storage of a container | The storage of its root disk, read from the backup. | `-s`, `--storage` |

pvecli reads the root disk storage of a container from the backup because Proxmox VE would otherwise put every volume of the container on `local`. Here the root disk was on the Ceph storage `ceph-vm`, and `--start` starts the container once it is restored:

--8<-- "screens/backups-restore-ct.html"

## Over an existing guest

If the ID is taken, pvecli stops before anything is changed:

--8<-- "screens/backups-restore-exists.html"

`--force` restores over the existing guest. The guest must be stopped, be of the same type as the backup (VM or container) and live on a node that can read the backup. pvecli asks you to type the ID again:

--8<-- "screens/backups-restore-force.html"

!!! danger "The current disks of the guest are destroyed"
    `--force` replaces the configuration and every disk of the guest with those of the backup. Everything written to the guest since the backup is lost. With `-y`, pvecli does not ask for the ID. If you are not sure which state you need, restore as a new guest first and compare.

