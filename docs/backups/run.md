---
icon: lucide/play
title: Back up guests now
description: Back up one guest or many at once, to the storage you choose, and check that each one was really saved.
---

# Back up guests now

Pass the guest IDs to [backup run](../cli/backup/run.md), and the backup storage with `--storage`:

--8<-- "screens/backups-run.html"

pvecli starts one vzdump task per node, on all the nodes at the same time, and waits for them. A guest counts as backed up only when two checks agree: the task log says its backup finished, and a new archive of it is on the storage. A vzdump task can end OK and still have skipped a guest (one that moved to another node during the run, for example), so the task status alone is not trusted.

## Pick the guests

| Selection | Guests |
|---|---|
| `100,101` or `100-110` | These IDs, templates included. |
| `--tag web` | The guests carrying the tag. |
| `--pool prod` | The guests of the resource pool. |
| `--node pve2` | The guests currently on the node. |
| `--all` | Every guest of the cluster except templates. |

The filters (`--tag`, `--pool`, `--node`, `--all`) skip templates and can be combined: `--tag web --node pve2` takes the guests with the tag that are on pve2.

```bash
pvecli backup run --tag web --storage pbs -p prod
```

## Storage, mode and compression

| Option | Default | Values |
|---|---|---|
| `-s`, `--storage` | The default storage of each node's vzdump configuration, else `local` | Any enabled storage that holds backups and is available on the guest's node. |
| `--mode` | `snapshot` | `snapshot` (the guest keeps running), `suspend`, `stop` (the guest is shut down for the backup and started again if it was running). |
| `--compress` | `zstd` | `zstd`, `gzip`, `lzo` or `0` (none). A PBS storage ignores it and compresses on its own. |

pvecli refuses the run before starting anything if the storage does not hold backups or is not available on one of the nodes.

## Protect the backup or add notes

`--protected` marks the new backups so that neither pruning nor removal deletes them. `--notes` sets their notes; `{{guestname}}`, `{{vmid}}` and `{{node}}` are replaced for each guest. With `-y`, pvecli does not ask before starting:

--8<-- "screens/backups-run-protected.html"

The backups appear with their protection and notes in [backup list](../cli/backup/list.md):

--8<-- "screens/backups-list-protected.html"

To remove the protection later, see [Prune and protect backups](retention.md).

## Older backups are kept

A run started by pvecli never prunes older backups, even when the storage has a retention setting (a vzdump started another way prunes by that retention by default). To apply a retention, prune the storage with `pvecli backup prune` (see [Prune and protect backups](retention.md)) or give the retention to a [scheduled job](jobs.md).

## When a guest is not backed up

The table shows `FAILED` and the reason, pvecli prints an error for each such guest, and the command exits with code 1 (see [Exit codes](../reference/exit-codes.md)):

```text
┏━━━━━┳━━━━━━━━━━┳━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ ID  ┃ Name     ┃ Node ┃ Result ┃ Archive or reason                        ┃
┡━━━━━╇━━━━━━━━━━╇━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 820 │ dns-01   │ pve1 │ OK     │ pbs:backup/ct/820/2026-11-19T08:50:47Z   │
│ 822 │ web-02   │ pve2 │ FAILED │ absent from the task log, not backed up  │
└─────┴──────────┴──────┴────────┴──────────────────────────────────────────┘
Error: 822 (web-02): absent from the task log, not backed up
Warning: 1/2 guest(s) backed up
```

`absent from the task log` means vzdump never started the backup of that guest, usually because it left the node between the moment pvecli read the cluster and the start of the task. Run the command again for that guest.
