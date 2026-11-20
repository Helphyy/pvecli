---
icon: lucide/archive
title: Backups
description: Check, run, restore, schedule and prune vzdump backups on Proxmox VE storage and on Proxmox Backup Server.
hide:
  - toc
---

# Backups

pvecli works with the vzdump backups of your guests, wherever they are stored: a directory or NFS storage of the cluster, or a Proxmox Backup Server (PBS) datastore added as a storage. It finds them on every enabled storage that holds backups, reads a shared storage once and a local one on each node. The commands that back up, restore or delete show what they touch and ask first (`-y` skips the question).

<div class="sketch" markdown>

```mermaid
---
config:
  look: handDrawn
  flowchart:
    curve: basis
---
flowchart TB
  you(["Your workstation<br/>pvecli"]):::pv
  job["backup job<br/>nightly, 02:00"]:::job
  subgraph prod ["cluster prod#nbsp;"]
    direction LR
    vm1["web-01"]:::vm -.- p1["pve1<br/>vzdump"]:::host
    p2["pve2<br/>vzdump"]:::host -.- ct1["dns-01"]:::ct
  end
  you -- "backup run" --> p1
  job -- "on schedule" --> p2
  p1 --> pbs[("PBS datastore")]:::store
  p2 --> pbs
  p2 --> loc[("local storage<br/>of the node")]:::store
  you -. "restore, files, prune" .-> pbs

  classDef pv fill:#ffae0b,stroke:#b35800,color:#1f2328,stroke-width:2px
  classDef host fill:#a5d8ff,stroke:#1971c2,color:#1f2328
  classDef vm fill:#b2f2bb,stroke:#2f9e44,color:#1f2328
  classDef ct fill:#d0bfff,stroke:#6741d9,color:#1f2328
  classDef store fill:#ffc9c9,stroke:#e03131,color:#1f2328
  classDef job fill:#fff3bf,stroke:#f08c00,color:#1f2328
```

</div>

A backup is made by vzdump on the node of the guest, started by you (`backup run`) or by a scheduled job, and written to a backup storage. A job can prune the older backups after each run; `backup prune` does it on demand.

<div class="grid cards" markdown>

-   :lucide-shield-check:{ .lg } __Check backup coverage__

    ---

    The guests without a job, without a recent backup or whose last run failed.

    [:lucide-arrow-right: Check backup coverage](coverage.md)

-   :lucide-play:{ .lg } __Back up guests now__

    ---

    One guest or many, to the storage you choose, each one checked.

    [:lucide-arrow-right: Back up guests now](run.md)

-   :lucide-archive-restore:{ .lg } __Restore a backup__

    ---

    As a new guest, or over a stopped one.

    [:lucide-arrow-right: Restore a backup](restore.md)

-   :lucide-folder-open:{ .lg } __Restore files from a PBS backup__

    ---

    Browse a PBS backup to find a file, without restoring the guest.

    [:lucide-arrow-right: Restore files from a PBS backup](restore-files.md)

-   :lucide-calendar-clock:{ .lg } __Schedule backup jobs__

    ---

    Create, change, pause, run and delete the jobs of a cluster.

    [:lucide-arrow-right: Schedule backup jobs](jobs.md)

-   :lucide-trash-2:{ .lg } __Prune and protect backups__

    ---

    Retention rules, protection, notes and removal.

    [:lucide-arrow-right: Prune and protect backups](retention.md)

</div>

[Check backup coverage](coverage.md){ .md-button .md-button--primary }
[backup commands](../cli/backup/index.md){ .md-button }
