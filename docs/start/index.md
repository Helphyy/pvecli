---
icon: lucide/rocket
title: Getting started
description: Install pvecli, connect it to a Proxmox VE node or cluster and run your first commands.
hide:
  - toc
---

# Getting started

pvecli runs on your workstation with Python 3.10 or later and talks to the API of Proxmox VE 8 or 9: a single node, or a cluster. Nothing is installed on the nodes. You need an API token, or a user and password.

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
  you(["Your workstation<br/>pvecli + config.yaml"]):::pv
  you -- "HTTPS pve:8006<br/>token or password" --> solo
  you -. "or one node<br/>of a cluster" .-> p1
  subgraph home ["a single node (profile home)#nbsp;"]
    solo["pve"]:::host
    solo -.- vm1["web-01"]:::vm
    solo -.- ct1["dns-01"]:::ct
  end
  subgraph prod ["a cluster (profile prod)#nbsp;"]
    direction TB
    p1["pve1"]:::host --- lan(("cluster<br/>network")):::net
    lan --- p2["pve2"]:::host
    lan --- p3["pve3"]:::host
  end

  classDef pv fill:#ffae0b80,stroke:#e57000,stroke-width:2px
  classDef host fill:#4dabf74d,stroke:#1c7ed6
  classDef vm fill:#51cf664d,stroke:#2f9e44
  classDef ct fill:#9775fa4d,stroke:#7048e8
  classDef net fill:#fcc41940,stroke:#f08c00
  classDef zone fill:#e570000d,stroke:#e57000,stroke-dasharray:6 4
  class home,prod zone
```

</div>

pvecli sends its API requests to the node of the profile. On a single node, that is all there is. In a cluster, that node answers for all of them: every node, VM and container.

Follow these pages in order:

<div class="grid cards" markdown>

-   :lucide-download:{ .lg } __Install pvecli__

    ---

    Install with pipx or pip, check the version, turn on shell completion.

    [:lucide-arrow-right: Install pvecli](install.md)

-   :lucide-key-round:{ .lg } __Connect to Proxmox VE__

    ---

    Create an API token, save it in a profile, test it.

    [:lucide-arrow-right: Connect to Proxmox VE](connect.md)

-   :lucide-square-terminal:{ .lg } __First commands__

    ---

    Look at the cluster, its nodes and guests, one guest in detail.

    [:lucide-arrow-right: First commands](first-commands.md)

</div>

[Install pvecli](install.md){ .md-button .md-button--primary }

Reference: [configuration file](../reference/config-file.md), [permissions](../reference/permissions.md), [all commands](../cli/index.md), [config](../cli/config/index.md).
