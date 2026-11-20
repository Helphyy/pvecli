---
icon: lucide/calendar-clock
title: Schedule backup jobs
description: Create, change, pause, run and delete the scheduled backup jobs of a cluster, with their retention.
---

# Schedule backup jobs

A backup job is a vzdump run that Proxmox VE starts on schedule, on every node, for the guests it selects. Jobs are cluster-wide: they are the same ones you see under **Datacenter > Backup** in the web interface.

## Create a job

[backup jobs add](../cli/backup/jobs.md#add) needs a schedule, a storage and one selection of guests:

--8<-- "screens/backups-jobs-add.html"

| Selection | Guests |
|---|---|
| `--vmid 100,101` | These IDs. |
| `--pool prod` | The guests of the resource pool, including those added to it later. |
| `--all` | Every guest of the cluster, including those created later. Add `--exclude 9000,9001` to leave some out. |

`--schedule` takes a Proxmox VE calendar event: `02:00` (every day), `sat 03:30`, `mon..fri 22:00`, `*:0/30` (every 30 minutes). `--id` names the job; without it, Proxmox VE generates an ID that starts with `backup-`.

The other options:

| Option | Default | Meaning |
|---|---|---|
| `--mode` | `snapshot` | `snapshot`, `suspend` or `stop`. |
| `--compress` | `zstd` | `zstd`, `gzip`, `lzo` or `0`. Ignored by a PBS storage. |
| `--keep-last`, `--keep-daily`, `--keep-weekly`, `--keep-monthly`, `--keep-yearly` | the retention of the storage | How many backups of each guest the job keeps after each run. |
| `--notes-template` | | Notes of each backup; `{{guestname}}`, `{{vmid}}` and `{{node}}` are replaced. |
| `--comment` | | Description of the job. |
| `--node` | every node | Run the job on this node only. |
| `--disabled` | | Create the job disabled. |

## List the jobs

[backup jobs list](../cli/backup/jobs.md#list) shows the schedule, the selection, the retention and the next run of each job. `storage` in the Retention column means the job follows the retention of its storage.

--8<-- "screens/backups-jobs-list.html"

It takes several profiles with `-p` to show the jobs of several clusters (see [Run a command on several clusters](../fleet/several-clusters.md)).

## Change a job

[backup jobs edit](../cli/backup/jobs.md#edit) sends only the options you give; the others keep their value. A new selection replaces the old one, whatever its kind: `--vmid` on a job created with `--all` drops `--all` and its exclusions.

--8<-- "screens/backups-jobs-edit.html"

## Pause a job

[backup jobs disable](../cli/backup/jobs.md#disable) keeps the job but stops its schedule; [backup jobs enable](../cli/backup/jobs.md#enable) starts it again. A disabled job shows `no` and `disabled` in the list:

--8<-- "screens/backups-jobs-disable.html"

--8<-- "screens/backups-jobs-list-disabled.html"

Its guests show `nightly (disabled)` in the Job column of `pvecli backup status` (see [Check backup coverage](coverage.md)).

## Run a job now

[backup jobs run](../cli/backup/jobs.md#run) runs the job immediately, with its own options, on the guests it selects today, then checks each guest as `pvecli backup run` does (see [Back up guests now](run.md)). It also runs a disabled job.

--8<-- "screens/backups-jobs-run.html"

Unlike `pvecli backup run`, this run applies the retention of the job (or of its storage when the job has none): after it, the older backups of these guests are pruned, as after a scheduled run. Here the `pbs` backups of 820, 821 and 822 made earlier the same day were removed, since `--keep-daily 7` keeps one per day.

Proxmox VE has no "run this job" call in its API: pvecli starts vzdump on each node with the options of the job. Only `root@pam` may tie that run to the job ID; with another user, pvecli prints `job-id refused` and runs the same backup without the link (see [Permissions](../reference/permissions.md)).

## Delete a job

[backup jobs remove](../cli/backup/jobs.md#remove) deletes the job after confirmation. The backups it made stay on their storage.

--8<-- "screens/backups-jobs-remove.html"
