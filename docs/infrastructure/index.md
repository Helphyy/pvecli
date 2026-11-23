---
icon: lucide/server
title: Infrastructure
description: Run the cluster itself with pvecli, from its health and capacity to its nodes, storage, SDN and Ceph.
hide:
  - toc
---

# Infrastructure

This section covers the cluster itself: its health and capacity, its nodes, their storage, the SDN and Ceph. It also covers the operations that touch every node at once: maintenance, a full shutdown or reboot, and building a cluster from standalone nodes. On call, start with [Check cluster health](health.md).

<div class="sketch" markdown>

```mermaid
---
config:
  look: handDrawn
  flowchart:
    curve: basis
    nodeSpacing: 22
    rankSpacing: 40
---
flowchart TB
  you(["Your workstation<br/>pvecli"]):::pv
  you -- "HTTPS :8006, API token<br/>to one node" --> prod
  subgraph prod ["cluster prod#nbsp;"]
    direction TB
    %% Declared last to first: the layout lays the nodes above the hub out in reverse
    p3["pve3"]:::host --- lan(("corosync<br/>link 0")):::net
    p2["pve2"]:::host --- lan
    p1["pve1"]:::host --- lan
    lan --- p4["pve4"]:::host
    lan --- p5["pve5"]:::host
  end
  prod -.- local[("local,<br/>local-lvm")]:::store
  prod -.- ceph[("Ceph<br/>pool ceph-vm")]:::store
  prod -.- sdn["SDN zvlan<br/>vnet vvlan"]:::net
  sdn -.- vm1["web-01"]:::vm
  prod -.- pbs[("PBS<br/>backups")]:::store

  classDef pv fill:#ffae0b80,stroke:#e57000,stroke-width:2px
  classDef host fill:#4dabf74d,stroke:#1c7ed6
  classDef vm fill:#51cf664d,stroke:#2f9e44
  classDef net fill:#fcc41940,stroke:#f08c00
  classDef store fill:#ff6b6b40,stroke:#e03131
  classDef zone fill:#e570000d,stroke:#e57000,stroke-dasharray:6 4
  class prod zone
```

</div>

pvecli talks to one node of the cluster through the API, and that node answers for all of them: [cluster](../cli/cluster/index.md) and [node](../cli/node/index.md) for the nodes and corosync, [storage](../cli/storage/index.md) for the storages, [sdn](../cli/sdn/index.md) for the zones and vnets guests attach to, [ceph](../cli/ceph/index.md) for the Ceph cluster the nodes run.

<div class="grid cards" markdown>

-   :lucide-activity:{ .lg } __Check cluster health__

    ---

    Quorum, failure tolerance, HA and Ceph in one view, then what is wrong.

    [:lucide-arrow-right: Check cluster health](health.md)

-   :lucide-gauge:{ .lg } __Check capacity and usage__

    ---

    What the nodes consume, what the guests are given, how full the storage is.

    [:lucide-arrow-right: Check capacity and usage](usage.md)

-   :lucide-wrench:{ .lg } __Put a node in maintenance__

    ---

    Empty a node, keep Ceph quiet, work on it, bring it and its guests back.

    [:lucide-arrow-right: Put a node in maintenance](node-maintenance.md)

-   :lucide-power-off:{ .lg } __Shut down or reboot a cluster__

    ---

    Every node in order, with HA and Ceph kept quiet, then the way back.

    [:lucide-arrow-right: Shut down or reboot a cluster](cluster-power.md)

-   :lucide-network:{ .lg } __Create or join a cluster__

    ---

    Turn a standalone node into a cluster, then add nodes to it.

    [:lucide-arrow-right: Create or join a cluster](create-join.md)

-   :lucide-hard-drive:{ .lg } __Add and manage storage__

    ---

    Shared storage checked from every node, ISO images, templates.

    [:lucide-arrow-right: Add and manage storage](storage.md)

-   :lucide-waypoints:{ .lg } __Manage SDN zones and vnets__

    ---

    Zones, vnets and subnets, then apply or discard the changes.

    [:lucide-arrow-right: Manage SDN zones and vnets](sdn.md)

-   :lucide-database:{ .lg } __Inspect Ceph__

    ---

    Health, OSDs, pools, flags, versions and the Ceph log.

    [:lucide-arrow-right: Inspect Ceph](ceph.md)

</div>

[Check cluster health](health.md){ .md-button .md-button--primary }
