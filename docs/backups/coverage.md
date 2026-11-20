---
icon: lucide/shield-check
title: Check backup coverage
description: Find the guests that no job covers, that have no recent backup or whose last backup run failed.
---

# Check backup coverage

[backup status](../cli/backup/status.md) shows, for each guest of the cluster, the job that covers it, the age of its newest backup and the result of its last backup run.

--8<-- "screens/backups-status.html"

| Column | What it says |
|---|---|
| Job | The scheduled jobs that include the guest, `(disabled)` after a job that is off, or `no job`. |
| Last backup | Age of the newest backup of the guest on any backup storage of the cluster, or `never`. |
| Last run | Result of the last vzdump task that included the guest (`OK` or `FAILED`) and its age, or `-` when none is left in the task history of the node. |
| Detail | Why the last run failed, or why a value could not be checked. |

The last run comes from the vzdump tasks of each node; for a task that backed up several guests, from its log, guest by guest. It can differ from the last backup: a run that succeeded and whose archive was removed since shows `OK` with an older or missing last backup.

## Only the guests that need attention

`--older-than` keeps the guests whose newest backup is older than the given age (`12h`, `2d`, `1w`) or missing. `--failed` keeps the guests whose last run failed. Given together, a guest with either problem is shown.

--8<-- "screens/backups-status-older.html"

To check several clusters in one command, pass several profiles with `-p` (the forms are in [Run a command on several clusters](../fleet/several-clusters.md)):

```bash
pvecli backup status --older-than 2d --failed -p prod,staging
```

## List the backups

[backup list](../cli/backup/list.md) lists the backups of every guest, on every backup storage, newest first for each guest. Narrow it with `--vmid` (`100,101` or `100-110`), `--tag`, `--pool` or `--storage`.

--8<-- "screens/backups-list.html"

The volid in the first column is the name the other `backup` commands take. A guest that no longer exists shows `gone` in the Name column.

## When a value says "not checked"

Proxmox VE leaves out of its answers what your user may not see, without an error. Where a missing privilege hides backups, a storage or the task history of a node, pvecli prints a warning above the table, and `pvecli backup status` shows `not checked` instead of `never` or `-` in the cells it could not fill. The privileges involved are listed in [Permissions](../reference/permissions.md#results-that-depend-on-what-you-can-see).

## Guests without a job, across clusters

[find](../cli/find.md) `--no-backup` lists the guests that no backup job covers, using the same list as the Proxmox VE web interface. It takes the other filters of `find`, and several profiles:

--8<-- "screens/backups-find-no-backup.html"

To add the missing guests to a job, see [Schedule backup jobs](jobs.md).
