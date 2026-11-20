---
icon: lucide/gauge
title: Check capacity and usage
description: See what the nodes consume, what the guests are given, and how full the storage is.
---

# Check capacity and usage

[cluster usage](../cli/cluster/usage.md) puts the load of the nodes next to what the guests use and what they were given:

--8<-- "screens/infrastructure-usage.html"

1. **Hypervisor load**: what the nodes consume right now, out of the installed cores and memory.
2. **Guest workload**: what the guests use, then what they are allocated (templates excluded). The ratio is the overcommit: `1.40x the installed cores` means 14 vCPU are given on 10 physical cores.
3. **Overhead**: the part of the node load no guest accounts for (Ceph daemons, Proxmox VE services, page cache). On a Ceph cluster with small nodes it is most of the memory, as here.
4. **Provisioned**: the size of every guest disk, against `Used` (what is written) on the storages that hold guest disks.

The `Counted` column of the storage table says which storages enter the guest capacity: a storage without guest disk content (ISO, backups) or a Proxmox Backup Server (PBS) datastore is listed but not counted.

The storage scan reads the content of every storage holding guest disks. On a large cluster, `--no-storage` skips it (no provisioned figures), and `--storage NAME` scans one storage only.

## One node

[node info](../cli/node/info.md) shows the Proxmox VE version, CPU model, memory, root file system and swap of a node; `-a` shows every node:

--8<-- "screens/infrastructure-node-info.html"

## Storage

[storage list](../cli/storage/list.md) shows every storage as each node sees it: a shared storage appears once per node, a local one with the figures of that node. `-n` keeps one node:

--8<-- "screens/infrastructure-storage-list.html"

`-o usage` sorts by usage and moves that column first. For the Ceph pools behind an RBD storage, use [ceph pool](../cli/ceph/pool.md) (see [Inspect Ceph](ceph.md)).

## One resource pool

[pool usage](../cli/pool/usage.md) gives the same figures for the guests of one resource pool, and its share of the cluster and of all guests:

--8<-- "screens/infrastructure-pool-usage.html"

Pools themselves are set up in [Organize guests with tags and pools](../guests/tags-pools.md).

`cluster usage`, `storage list` and `pool usage` take `--json`, and `cluster usage` takes several profiles at once (see [Run a command on several clusters](../fleet/several-clusters.md)).
