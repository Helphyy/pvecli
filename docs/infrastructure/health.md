---
icon: lucide/activity
title: Check cluster health
description: Get quorum, failure tolerance, HA and Ceph in one view, then find what is wrong.
---

# Check cluster health

[cluster health](../cli/cluster/health.md) shows whether the cluster can still write, and how many more nodes it can lose:

--8<-- "screens/infrastructure-health.html"

| Item | What it tells you |
|:--|:--|
| Quorum | `quorate`: the cluster is writable. `lost`: `/etc/pve` is read-only on every node, no guest can be started, moved or changed. |
| Nodes | Online nodes out of the members, with the names of the offline ones. |
| Failure tolerance | Votes online, votes quorum needs, and how many more nodes can fail before quorum is lost. `none` means the next failure makes the cluster read-only. |
| QDevice | Whether an external arbiter votes. With a QDevice the tolerance shows `unknown`: the API does not expose the arbiter's votes. |
| HA resources | How many guests HA manages. On PVE 9.2 and later this line is `HA` and shows the state of the HA stack (`armed`, `standby`, `disarmed`). |
| Ceph | The overall Ceph health, or `absent` on a cluster without Ceph. |

The command exits with `1` when the cluster is not quorate, so a monitoring script can rely on the exit code alone (see [Exit codes](../reference/exit-codes.md)). `--json` gives the same data as a document.

## Find what is wrong

Each item of the health view has its own command.

### Ceph is not HEALTH_OK

[ceph status](../cli/ceph/status.md) lists the checks behind the warning, one per line under `Health`. [Inspect Ceph](ceph.md#health) shows its output, then the OSD tree, the pools and the Ceph log.

### A node is offline

[cluster status](../cli/cluster/status.md) shows the cluster entry (quorum, configuration version) and one line per member as corosync sees it:

--8<-- "screens/infrastructure-status.html"

[node list](../cli/node/list.md) adds the load of each node. An offline node has no figures:

--8<-- "screens/infrastructure-node-list.html"

### Something failed

[cluster tasks](../cli/cluster/tasks.md) lists the last tasks of every node, newest first. A failed task shows its error in red in the `Status` column:

--8<-- "screens/infrastructure-tasks.html"

`-l` sets how many tasks to show (50 by default) and `-r` keeps only the running ones, for instance a migration or a backup that does not end.

### Where a guest or a storage is

[cluster resources](../cli/cluster/resources.md) lists every guest, node, storage and SDN zone of the cluster with its node and status, one table per type. `-t vm`, `-t node` or `-t storage` keeps one type.

### SDN

[sdn health](../cli/sdn/health.md) reports SDN changes not applied yet and vnets missing on some nodes. See [Manage SDN zones and vnets](sdn.md).

## Before a maintenance

`cluster health` tells you whether the cluster is fine now. [cluster preflight](../cli/cluster/preflight.md) checks what breaks a maintenance or a join later: versions, clock drift, name resolution, core services, corosync links. See [Shut down or reboot a cluster](cluster-power.md#check-the-cluster-first).
