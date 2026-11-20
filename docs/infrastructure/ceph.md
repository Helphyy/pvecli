---
icon: lucide/database
title: Inspect Ceph
description: Read the state of the Ceph cluster of a Proxmox VE cluster, from its health to its OSDs, pools, flags, versions and log.
---

# Inspect Ceph

The `ceph` commands read the Ceph cluster that Proxmox VE runs on its nodes, through the API: no SSH, no `ceph` command on a node. On a cluster without Ceph they answer `No Ceph on this cluster`.

## Health

[ceph status](../cli/ceph/status.md) shows the overall health, each check behind a warning, the monitors, the OSDs up and in, the raw capacity and the flags set:

--8<-- "screens/infrastructure-ceph-status.html"

Each line under `Health` is a Ceph health check with its summary. Here every OSD reported slow BlueStore operations: its disk was slow to answer under load.

When the OSD counts differ (`4/5 up`) while Ceph reports `HEALTH_OK`, pvecli adds that the missing OSD is likely an empty slot left in the OSD map by a destroyed OSD, to be purged with `ceph osd purge`. When Ceph also warns, as here, the counts show in red.

## OSDs

[ceph osd](../cli/ceph/osd.md) lists the OSDs with their host, state, size, usage and device class:

--8<-- "screens/infrastructure-ceph-osd.html"

An OSD `down` but still `in` holds data Ceph cannot reach yet; once it is `out`, Ceph rebuilds that data on the other OSDs. Compare the `Used` column across OSDs: one much fuller than the others fills up first and blocks writes to its pools.

## Pools

[ceph pool](../cli/ceph/pool.md) lists the pools with their replication (`Size`, `Min size`), placement groups, usage and CRUSH rule:

--8<-- "screens/infrastructure-ceph-pool.html"

A pool stops accepting writes when fewer than `Min size` copies of its data are available. The RBD storage that guests use is one of these pools (`ceph-vm` here). For the storage as Proxmox VE sees it, see [Check capacity and usage](usage.md).

## Flags

[ceph flags](../cli/ceph/flags.md) lists every OSD flag with its meaning and whether it is set. `ceph status` warns as long as one is set. Setting and clearing flags around a shutdown or a maintenance is covered in [Shut down or reboot a cluster](cluster-power.md#clear-the-ceph-flags).

## Versions after an upgrade

[ceph versions](../cli/ceph/versions.md) lists the Ceph version of every daemon and every node:

--8<-- "screens/infrastructure-ceph-versions.html"

During an upgrade, a daemon still on the old version keeps running until it restarts. When the table holds more than one version, pvecli warns to finish the upgrade before any other maintenance.

## Log

[ceph log](../cli/ceph/log.md) prints the end of the Ceph cluster log, read from a node (the first online node by name, or the one you give). `-l` sets the number of lines (50 by default). Errors show in red, warnings in yellow:

--8<-- "screens/infrastructure-ceph-log.html"

`ceph status`, `ceph osd`, `ceph pool`, `ceph flags` and `ceph versions` take `--json`.
