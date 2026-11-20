---
icon: lucide/layers
title: Fleet
description: Run pvecli on several clusters at once, find what owns an address or a disk, audit clusters, and run pvecli from scripts.
---

# Fleet

This section is for people who look after more than one cluster: one cluster per client, or prod and staging side by side. Each cluster is a profile, and `-p` takes a list of profiles, a pattern or a group: the command runs on each cluster and prints one block per profile. `find` and `audit` are as useful on a single cluster. The last page covers running pvecli unattended, from cron or CI.

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
  you -- "-p '*'" --> p1
  you -- "one API token<br/>per profile" --> s1
  you -- "HTTPS :8006" --> c1
  subgraph prod ["profile prod#nbsp;"]
    p1["pve1"]:::host --- p2["pve2"]:::host
  end
  subgraph staging ["profile staging#nbsp;"]
    s1["pve6"]:::host
  end
  subgraph clienta ["profile client-a#nbsp;"]
    c1["pve1"]:::host --- c2["pve2"]:::host
  end
  p2 --> out
  s1 --> out
  c2 --> out
  out["Your terminal: one block per profile,<br/>in the order of -p"]:::pv

  classDef pv fill:#ffae0b,stroke:#b35800,color:#1f2328,stroke-width:2px
  classDef host fill:#a5d8ff,stroke:#1971c2,color:#1f2328
```

</div>

<div class="grid cards" markdown>

-   :lucide-layers-2:{ .lg } __Run a command on several clusters__

    ---

    Lists, patterns and groups of profiles, failures, order and JSON.

    [:lucide-arrow-right: Several clusters](several-clusters.md)

-   :lucide-search:{ .lg } __Find a guest, an address or a disk__

    ---

    Which guest, node or storage owns an IP, a MAC, a volume or a name.

    [:lucide-arrow-right: Find](find.md)

-   :lucide-clipboard-check:{ .lg } __Audit a cluster__

    ---

    Certificates, access, firewall, backups, snapshots and orphan disks.

    [:lucide-arrow-right: Audit](audit.md)

-   :lucide-code:{ .lg } __Use pvecli in scripts__

    ---

    `--json`, `-y`, exit codes, cron and CI.

    [:lucide-arrow-right: Scripting](scripting.md)

</div>

[Run a command on several clusters](several-clusters.md){ .md-button .md-button--primary }

Command reference: [find](../cli/find.md), [audit](../cli/audit.md).
