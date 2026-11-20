---
icon: lucide/rocket
title: Getting started
description: Install pvecli, connect it to a Proxmox VE cluster and run your first commands.
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
---
flowchart TB
  you(["Your workstation<br/>pvecli + config.yaml"]):::pv
  you -- "HTTPS to pve1:8006<br/>API token or password" --> prod
  subgraph prod ["cluster prod (profile prod)#nbsp;"]
    direction LR
    p1["pve1"]:::host --- p2["pve2"]:::host --- p3["pve3"]:::host
    p2 -.- vm1["web-01"]:::vm
    p3 -.- ct1["dns-01"]:::ct
  end

  classDef pv fill:#ffae0b,stroke:#b35800,color:#1f2328,stroke-width:2px
  classDef host fill:#a5d8ff,stroke:#1971c2,color:#1f2328
  classDef vm fill:#b2f2bb,stroke:#2f9e44,color:#1f2328
  classDef ct fill:#d0bfff,stroke:#6741d9,color:#1f2328
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

Command reference: [config](../cli/config/index.md).
