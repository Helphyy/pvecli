---
icon: lucide/monitor
title: Guests
description: Create, run, reach, move, snapshot and organize the VMs and containers of a Proxmox VE cluster.
hide:
  - toc
---

# Guests

A guest is a VM or a container. This section covers their life on the cluster: creating them one at a time or from a file, starting and stopping them, opening a console, moving them between nodes, taking snapshots, putting them under HA, and sorting them with tags and pools. The VM and container commands mirror each other: `pvecli vm start` starts a VM, `pvecli ct start` a container, and so on for each command that applies to both.

<div class="sketch" markdown>

```mermaid
---
config:
  look: handDrawn
  flowchart:
    curve: basis
---
flowchart TB
  you(["Your workstation<br/>pvecli + guests.yaml"]):::pv
  you -- "HTTPS :8006, API token" --> prod
  you -. "vnc, ssh, exec" .-> web
  subgraph prod ["cluster prod#nbsp;"]
    direction LR
    tpl["template 819<br/>cloud-init"]:::vm -- "vm deploy" --> web["web-02"]:::vm
    img[("LXC image")]:::store -- "ct deploy" --> dns["dns-01"]:::ct
    web -.- p1["pve1"]:::host
    dns -.- p1
    web -- "vm migrate,<br/>HA restart" --> p2["pve2"]:::host
    web -.- ceph[("Ceph<br/>disks, snapshots")]:::store
  end

  classDef pv fill:#ffae0b80,stroke:#e57000,stroke-width:2px
  classDef host fill:#4dabf74d,stroke:#1c7ed6
  classDef vm fill:#51cf664d,stroke:#2f9e44
  classDef ct fill:#9775fa4d,stroke:#7048e8
  classDef store fill:#ff6b6b40,stroke:#e03131
  classDef zone fill:#e570000d,stroke:#e57000,stroke-dasharray:6 4
  class prod zone
```

</div>

<div class="grid cards" markdown>

-   :lucide-monitor-cog:{ .lg } __Create a VM__

    ---

    From an ISO, from a cloud-init template in one command, or as a clone.

    [:lucide-arrow-right: Create a VM](create-vm.md)

-   :lucide-box:{ .lg } __Create a container__

    ---

    Download an LXC image, then create, start and reach a container.

    [:lucide-arrow-right: Create a container](create-container.md)

-   :lucide-file-code:{ .lg } __Deploy guests from a YAML file__

    ---

    Describe VMs and containers in a file, clones or ISO installs, check it, create what is missing.

    [:lucide-arrow-right: Deploy guests from a YAML file](deploy-yaml.md)

-   :lucide-power:{ .lg } __Start, stop and reboot guests__

    ---

    One guest, a list, a range, or every guest of a tag, a pool or a node.

    [:lucide-arrow-right: Start, stop and reboot guests](power.md)

-   :lucide-terminal:{ .lg } __Open a console or a shell__

    ---

    VNC in the browser, SSH or RDP with the address found for you, commands through the agent.

    [:lucide-arrow-right: Open a console or a shell](console.md)

-   :lucide-arrow-right-left:{ .lg } __Migrate a guest__

    ---

    Move a VM live or a container with a restart, and what blocks a migration.

    [:lucide-arrow-right: Migrate a guest](migrate.md)

-   :lucide-camera:{ .lg } __Take and roll back snapshots__

    ---

    Snapshot one guest or many before a change, roll back, prune the old ones.

    [:lucide-arrow-right: Take and roll back snapshots](snapshots.md)

-   :lucide-heart-pulse:{ .lg } __Make a guest highly available__

    ---

    Restart a guest on another node when its node fails, on PVE 8 and 9.

    [:lucide-arrow-right: Make a guest highly available](ha.md)

-   :lucide-tags:{ .lg } __Organize guests with tags and pools__

    ---

    Tag and color guests, group them in pools, copy both to another cluster.

    [:lucide-arrow-right: Organize guests with tags and pools](tags-pools.md)

</div>

Reference: [deploy file format](../reference/deploy-file.md), [vm](../cli/vm/index.md), [ct](../cli/ct/index.md), [deploy](../cli/deploy.md), [snapshot](../cli/snapshot/index.md), [tag](../cli/tag/index.md), [pool](../cli/pool/index.md).

[Create a VM](create-vm.md){ .md-button .md-button--primary }
