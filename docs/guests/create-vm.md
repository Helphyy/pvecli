---
icon: lucide/monitor-cog
title: Create a VM
description: Create a VM from an ISO, deploy one from a cloud-init template in one command, or clone an existing one.
---

# Create a VM

Install from an ISO with `pvecli vm add`, get a ready-to-use VM from a cloud-init template with `pvecli vm deploy`, or copy a VM or a template with `pvecli vm clone`.

## From an ISO

Give [vm add](../cli/vm/add.md) a node and nothing else, and it asks for each setting in turn, with the cluster's storages, ISOs and bridges as menus:

<div class="cast" data-src="../../assets/casts/guests-vm-add-wizard.cast" data-poster="npt:0:15.6"></div>

After the summary and a `y`, the VM is created and pvecli offers to print the equivalent command with every option. Given those options, `vm add` asks nothing:

--8<-- "screens/guests-vm-add.html"

Without `--vmid`, the VM gets the next free ID. Options left out take these values:

| Setting | Without the option |
|:--|:--|
| CPU | 1 socket, 2 cores, type `x86-64-v2-AES` |
| Memory | 2048 MiB |
| Guest agent | enabled (`--no-agent` to disable) |
| Disk | none: pass both `--disk-storage` and `--disk-size` (GB) |
| Network | none: pass `--bridge` (and `--vlan`, `--firewall` if needed) |

`--name`, `--iso-storage`, `--iso` and `--os-type` are the four options that skip the wizard. For Windows, add `--os-version` (`11/2022/2025` by default) and `--virtio-iso-storage`, `--virtio-iso` for the VirtIO drivers. Windows 11, 2022 and 2025 also need `--tpm-storage` and `--efi-storage`: pvecli refuses the command without them.

## Make a template

A template is a VM that cannot run and serves as a source for clones. Stop the VM, then convert it with [vm template](../cli/vm/template.md):

--8<-- "screens/guests-vm-template.html"

The conversion cannot be undone, so pvecli asks first; a running VM is refused. To be used by `vm deploy`, the template should boot a cloud image (the Debian or Ubuntu "generic cloud" images, for example). Install `qemu-guest-agent` in it if you want `--wait-ip`, [vm ssh](../cli/vm/ssh.md) and [vm exec](../cli/vm/exec.md) to work on the VMs made from it.

## From a cloud-init template

[vm deploy](../cli/vm/deploy.md) clones the template, sizes the clone, writes its cloud-init settings, starts it and, with `--wait-ip`, waits until the guest agent reports its address:

--8<-- "screens/guests-vm-deploy.html"

- The clone is linked to the template: it takes a few seconds and almost no space. `--full` makes a full copy, `--storage` a full copy on another storage.
- `--disk-size` grows the boot disk. A disk never shrinks: a size below the template's is refused.
- `--user`, `--ssh-key` (repeatable, a file or the key itself), `--ip` (CIDR or `dhcp`), `--gw`, `--dns` and `--search-domain` go to cloud-init. A password is read from the environment variable named by `--password-env`, never from the command line.
- `--no-start` leaves the VM stopped.

Every value is checked against the cluster before the clone starts, and every problem is reported at once:

--8<-- "screens/guests-vm-deploy-check.html"

Nothing is created when a check fails.

## Clone a VM or a template

With a new ID and a name, [vm clone](../cli/vm/clone.md) asks nothing:

--8<-- "screens/guests-vm-clone.html"

Without them, it runs a wizard (ID, name, pool, tags, start at boot, linked or full) and ends with the command to clone the same way again.

| Source | Clone |
|:--|:--|
| Template | Linked by default, full copy with `--full`. |
| VM | Always a full copy (Proxmox VE does not link a clone to a VM). |

`--target` puts the clone on another node. Proxmox VE accepts it only when the source's disks are on shared storage (Ceph RBD, NFS...); a linked clone then stays on the template's storage, next to its base disk.

A clone made this way keeps the cloud-init settings of its source. Change them with `vm cloudinit`.

## Change the cloud-init settings

[vm cloudinit set](../cli/vm/cloudinit.md#set) takes the same cloud-init options as `vm deploy` and rebuilds the cloud-init drive. A running VM gets the change at its next reboot:

--8<-- "screens/guests-vm-cloudinit-set.html"

[vm cloudinit show](../cli/vm/cloudinit.md#show) prints the settings, with the changes not applied yet in the Pending column:

--8<-- "screens/guests-vm-cloudinit-show.html"

A VM without a cloud-init drive ignores these settings. `cloudinit set` adds the drive on the storage of the VM's first disk, or on `--drive-storage`.

## Remove a VM

[vm remove](../cli/vm/remove.md) asks before it deletes a VM and its disks. A running VM is refused unless you pass `--force`, which stops it first. `--purge` also removes the VM from the backup jobs and the HA resources that name it:

--8<-- "screens/guests-vm-remove.html"
