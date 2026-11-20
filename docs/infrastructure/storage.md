---
icon: lucide/hard-drive
title: Add and manage storage
description: Add a shared storage checked from every node, set what it holds, manage its ISO images and templates, remove it.
---

# Add and manage storage

[storage add](../cli/storage/add.md) adds a shared storage to the cluster: NFS, SMB/CIFS, Ceph RBD or CephFS of an external Ceph cluster, Proxmox Backup Server (PBS) or iSCSI. Unlike the web UI, it asks every node whether the backend answers before creating the storage (NFS, SMB/CIFS, PBS, iSCSI), and checks afterwards that each node reports it active.

## Add an NFS share

```bash
pvecli storage add nfs-iso -t nfs --server 10.0.0.30 --export /srv/iso -c iso,vztmpl -p prod
```

pvecli asks every node at once for the exports of the server, shows which nodes reach it, then the summary. After you confirm, it creates the storage and waits until each node reports it active:

```text
Scanning nfs on 10.0.0.30 from 5 node(s)...
   Backend visibility
┏━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┓
┃ Node ┃ Reachable ┃ Entries ┃
┡━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━┩
│ pve1 │ yes       │       2 │
│ pve2 │ yes       │       2 │
│ pve3 │ yes       │       2 │
│ pve4 │ yes       │       2 │
│ pve5 │ yes       │       2 │
└──────┴───────────┴─────────┘

═══ Summary ═══

  Storage: nfs-iso
  Type:    nfs (NFS share)
  server: 10.0.0.30
  export: /srv/iso
  content: iso,vztmpl
  Applies to: every node in the cluster

Create this storage? [y/n] (y): y
✓ Storage 'nfs-iso' created

Checking the storage on each node...
        Status of 'nfs-iso'
┏━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━┓
┃ Node ┃ Active ┃  Total ┃ Available ┃
┡━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━┩
│ pve1 │ yes    │ 1.9 TB │    1.2 TB │
│ pve2 │ yes    │ 1.9 TB │    1.2 TB │
│ pve3 │ yes    │ 1.9 TB │    1.2 TB │
│ pve4 │ yes    │ 1.9 TB │    1.2 TB │
│ pve5 │ yes    │ 1.9 TB │    1.2 TB │
└──────┴────────┴────────┴───────────┘
✓ Storage 'nfs-iso' is active on all 5 node(s)
```

Without `-t`, pvecli asks for every value in turn, with menus for the type, the export and the content types. Without `--export` (`--share`, `--datastore`, `--target` for the other types), pvecli takes the only entry the scan found, or shows a menu when there are several (with `-y` it stops instead).

### If some nodes cannot reach it

When no node reaches the server, nothing is created:

--8<-- "screens/infrastructure-storage-unreachable.html"

When only some nodes reach it, pvecli names the others and offers to restrict the storage to the nodes that can reach it. `-n pve1,pve2` restricts it from the start. If the storage is created but stays inactive on some nodes, pvecli says on which ones; if it is active nowhere, it prints the `storage remove` command to take it back.

### Other backends

| Type | Required | Discovery |
|:--|:--|:--|
| `nfs` | `--server`, `--export` | Exports seen by each node. |
| `cifs` | `--server`, `--share`; `--username` and `--password-env` for a protected share | Shares seen by each node. |
| `pbs` | `--server`, `--datastore`, `--username`, `--password-env`; `--fingerprint` for a self-signed certificate | Datastores, only with user and password. |
| `iscsi` | `--portal`, `--target` | Targets seen by each node. |
| `rbd` | `--monhost`, `--pool` (default `rbd`), `--keyring-file` | None. |
| `cephfs` | `--monhost`, `--fs-name` (default `cephfs`), `--keyring-file` | None. |

Passwords come from an environment variable named by `--password-env`, never from the command line. For a PBS with a self-signed certificate, run the command once without `--fingerprint`: if the server answers, pvecli prints the fingerprint it presents and the option to add. Compare it with the one shown on the PBS dashboard before using it.

`rbd` and `cephfs` connect to an external Ceph cluster through its monitors. Pass the keyring as a file (`--keyring-file`): without it the storage is created but stays inactive until the key is in place.

`-c` sets the content types. Each type accepts only some of them (`images` and `rootdir` for RBD, `backup` only for PBS); without `-c`, pvecli uses the usual set of the type.

## See what a storage holds

[storage list](../cli/storage/list.md) lists every storage of every node (see [Check capacity and usage](usage.md)). [storage info](../cli/storage/info.md) details one of them on one node:

--8<-- "screens/infrastructure-storage-info.html"

To change the content types of an existing storage, run [storage config](../cli/storage/config.md): it shows the current types in a checklist (++space++ to toggle, ++enter++ to confirm), then the change before and after, and asks before applying it. Here `snippets` is added to `local`, then the change is refused:

<div class="cast" data-src="../../assets/casts/infrastructure-storage-config.cast" data-poster="npt:0:9" data-rows="24"></div>

## ISO images and templates

[storage content list](../cli/storage/content.md#list) lists the volumes of a storage on a node; `-t iso`, `-t vztmpl` or `-t backup` keeps one type:

--8<-- "screens/infrastructure-content-list.html"

[storage content download](../cli/storage/content.md#download) makes the node download a file from a URL straight into the storage. The content type comes from the file extension (`.iso` and `.img` for `iso`, `.tar.zst`, `.tar.gz`, `.tar.xz` for `vztmpl`, `.qcow2`, `.raw`, `.vmdk`, `.ova` for `import`), the checksum algorithm from the length of `-c`:

--8<-- "screens/infrastructure-content-download.html"

`-b` starts the download and returns at once; follow it with `pvecli cluster tasks -r`. [storage content add](../cli/storage/content.md#add) uploads a file from your machine instead (`-s FILE`, with `-t iso`, `vztmpl` or `import`).

[storage content remove](../cli/storage/content.md#remove) deletes a volume:

--8<-- "screens/infrastructure-content-remove.html"

## Remove a storage

[storage remove](../cli/storage/remove.md) removes the definition from the cluster configuration. The data on the backend (the NFS export, the PBS datastore, the Ceph pool) stays as it is:

--8<-- "screens/infrastructure-storage-remove.html"

Guest disks and backups on that storage become unreachable from Proxmox VE until you add it again with the same ID.
