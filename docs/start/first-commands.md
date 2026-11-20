---
icon: lucide/square-terminal
title: First commands
description: Look around a cluster with pvecli, from its nodes to one guest in detail.
---

# First commands

Every command that talks to a cluster takes `-p` with the name of the profile to use. Without it, pvecli uses the default profile. The examples below run on the cluster of the profile `prod`.

## The cluster and its nodes

[cluster status](../cli/cluster/status.md) shows the cluster name, whether it has quorum, and its nodes:

--8<-- "screens/start-cluster-status.html"

[node list](../cli/node/list.md) adds the load of each node:

--8<-- "screens/start-node-list.html"

For a verdict on the state of the cluster (quorum, failure tolerance, HA, Ceph), see [Check cluster health](../infrastructure/health.md).

## VMs and containers

[vm list](../cli/vm/list.md) lists every VM of the cluster, whatever node it runs on. Running VMs show their CPU, memory and disk usage:

--8<-- "screens/start-vm-list.html"

[ct list](../cli/ct/list.md) does the same for containers:

--8<-- "screens/start-ct-list.html"

Both take `-n NODE` to keep the guests of one node and `-s running` or `-s stopped` to keep one state.

## Sort a list

`-o` (`--order`) sorts a list by a column and moves that column first. A prefix of the column name is enough:

--8<-- "screens/start-vm-list-order.html"

Usage columns sort by percentage, lowest first. Stopped guests use nothing and come first.

Without a column, `-o` prints the names it accepts:

--8<-- "screens/start-order-columns.html"

The `list` commands of nodes, storages, pools and tags take `-o` too.

## One guest in detail

[vm info](../cli/vm/info.md) shows the state, resources, configuration and network of a VM:

--8<-- "screens/start-vm-info.html"

The network section shows the addresses of the VM only when the QEMU guest agent runs inside it; without it, as here, it shows only the configured interfaces. [ct info](../cli/ct/info.md) shows the same sections for a container.

## Leave the ID out

When a command needs a guest and you do not give its ID, pvecli lists the guests in a menu. Move with the arrow keys, press ++enter++ to pick, ++esc++ to cancel:

<div class="cast" data-src="../../assets/casts/start-vm-info-menu.cast" data-poster="npt:0:8" data-rows="22"></div>

The same goes for nodes, storages and profiles. Where there is no terminal at all (cron, CI), the command stops with an error asking for the value instead: see [Use pvecli in scripts](../fleet/scripting.md).

## Where to go next

- [Create a VM](../guests/create-vm.md) or [a container](../guests/create-container.md)
- [Start, stop and reboot guests](../guests/power.md)
- [Check cluster health](../infrastructure/health.md)
- [Back up guests now](../backups/run.md) and [restore a backup](../backups/restore.md)
- [Run a command on several clusters](../fleet/several-clusters.md)
- [All commands](../cli/index.md)
