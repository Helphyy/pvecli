---
icon: lucide/rocket
title: Getting started
description: Install pvecli, connect it to a Proxmox VE cluster and run your first commands.
hide:
  - toc
---

# Getting started

pvecli runs on your workstation with Python 3.10 or later and talks to the API of a Proxmox VE 8 or 9 cluster. Nothing is installed on the nodes. You need an API token on the cluster, or a user and password.

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
  you -- "HTTPS pve1:8006<br/>token or password" --> p1
  subgraph prod ["cluster prod (profile prod)#nbsp;"]
    direction TB
    p1["pve1"]:::host --- lan(("cluster<br/>network")):::net
    lan --- p2["pve2"]:::host
    lan --- p3["pve3"]:::host
    p2 -.- vm1["web-01"]:::vm
    p3 -.- ct1["dns-01"]:::ct
  end

  classDef pv fill:#ffae0b80,stroke:#e57000,stroke-width:2px
  classDef host fill:#4dabf74d,stroke:#1c7ed6
  classDef vm fill:#51cf664d,stroke:#2f9e44
  classDef ct fill:#9775fa4d,stroke:#7048e8
  classDef net fill:#fcc41940,stroke:#f08c00
  classDef zone fill:#e570000d,stroke:#e57000,stroke-dasharray:6 4
  class prod zone
```

</div>

pvecli sends its API requests to one node, the one of the profile. That node answers for the whole cluster: every node, VM and container.

Follow these pages in order:

<div class="grid cards" markdown>

-   :lucide-download:{ .lg } __Install pvecli__

    ---

    Install with pipx or pip, check the version, turn on shell completion.

    [:lucide-arrow-right: Install pvecli](install.md)

-   :lucide-key-round:{ .lg } __Connect to a cluster__

    ---

    Create an API token, save it in a profile, test it.

    [:lucide-arrow-right: Connect to a cluster](connect.md)

-   :lucide-square-terminal:{ .lg } __First commands__

    ---

    Look at the cluster, its nodes and guests, one guest in detail.

    [:lucide-arrow-right: First commands](first-commands.md)

</div>

[Install pvecli](install.md){ .md-button .md-button--primary }

Reference: [configuration file](../reference/config-file.md), [permissions](../reference/permissions.md), [all commands](../cli/index.md), [config](../cli/config/index.md).
