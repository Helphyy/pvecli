---
icon: lucide/box
title: Create a container
description: Download an LXC image, then create a container from it with ct deploy or ct add, or clone an existing container.
---

# Create a container

A container is created from an LXC image stored on a node (a `.tar.zst` or `.tar.xz` file in a storage that holds container templates), or cloned from another container.

## Get an LXC image

[ct image list](../cli/ct/image.md#list) shows the images already on a storage of a node:

--8<-- "screens/guests-ct-image-list.html"

[ct image add](../cli/ct/image.md#add) downloads one from the Proxmox image repository. Give its file name with `--name`:

--8<-- "screens/guests-ct-image-add.html"

Without `--name`, pvecli lists the images of the repository in `fzf`, where you pick one or several (++tab++ selects, ++enter++ downloads). `fzf` must be installed for this. [ct image remove](../cli/ct/image.md#remove) deletes an image from a storage.

## Create a container in one command

[ct deploy](../cli/ct/deploy.md) creates the container, starts it and, with `--wait-ip`, waits for its IPv4 address. It ends with the command to create the same container again:

--8<-- "screens/guests-ct-deploy.html"

The source is either an image, given as a volume (`local:vztmpl/<file>`), or the ID of a container template, which is cloned.

| | From an image | From a container template |
|:--|:--|:--|
| Node | `--node` (needed on a cluster of several nodes) | The template's node |
| Root disk | `--storage`, else Proxmox VE's default `local`; `--disk-size` in whole GiB, 4G by default | Same storage, linked clone when it allows it; `--storage` or `--full` for a full copy; `--disk-size` only grows |
| Root password and SSH keys | `--password-env` (name of an environment variable) and `--ssh-key` | Those of the template |

Other options: `--cores`, `--memory`, `--swap`, `--ip` (CIDR, `dhcp` or `manual`), `--gw`, `--bridge` (vmbr0 by default), `--vlan`, `--dns`, `--features` (e.g. `nesting=1`), `--privileged`, `--tags`, `--pool`. As with `vm deploy`, every value is checked before anything is created.

## Create a container with ct add

[ct add](../cli/ct/add.md) with a node and no options runs a wizard, with the node's images, storages and bridges as menus. The root password it asks for is not echoed:

<div class="cast" data-src="../../assets/casts/guests-ct-add-wizard.cast" data-poster="npt:0:24"></div>

With `--hostname`, `--template-storage` and `--template`, it asks nothing:

--8<-- "screens/guests-ct-add.html"

`ct add` does not start the container (see [Start, stop and reboot guests](power.md)). The container is unprivileged unless you pass `--privileged`. `--password` sets the root password on the command line, where your shell history keeps it: prefer `ct deploy --password-env`.

## Clone a container

[ct clone](../cli/ct/clone.md) works like [vm clone](../cli/vm/clone.md): with `--newid` and `--hostname` it asks nothing, without them it runs a wizard.

--8<-- "screens/guests-ct-clone.html"

A clone of a container is a full copy. A clone of a container template is linked when its storage supports it (LVM-thin, ZFS, Ceph RBD, Btrfs) and a full copy otherwise, or with `--full`. [ct template](../cli/ct/template.md) converts a stopped container into a template.

## Remove a container

[ct remove](../cli/ct/remove.md) asks before it deletes a container and its disks. As with `vm remove`, a running container is refused unless you pass `--force`, `--purge` also drops it from the backup jobs and HA resources, and `-y` skips the question:

--8<-- "screens/guests-ct-remove.html"
