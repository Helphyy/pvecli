---
icon: lucide/file-text
title: Deploy file format
description: Every key of the YAML file read by pvecli deploy, for each kind of guest it creates, with its type, its default and what it does.
---

# Deploy file format

[deploy](../cli/deploy.md) reads one YAML file and creates the guests it lists. How to run it is in [Deploy guests from a YAML file](../guests/deploy-yaml.md).

## Top level

| Key | Type | Description |
|:--|:--|:--|
| `guests` | list | Required, not empty. One entry per guest, or per series of guests. |
| `defaults` | mapping | Optional. Keys every guest takes unless it sets its own, with `vm` and `ct` sub-mappings for one type only. |
| `x-...` | anything | Ignored by pvecli: a place for [YAML anchors](#yaml-anchors). |

Any other top-level key is an error.

## Four kinds of guests

`type` and the source key decide how a guest is made. The four kinds mix freely in one file, on one cluster or several:

| Kind | `type` | Source | pvecli | Inside the guest |
|:--|:--|:--|:--|:--|
| VM clone | `vm` | `template: 9000`, a template VM | Clones it (linked, or full), grows its disk, sets cloud-init | cloud-init: user, keys, address, DNS |
| VM from an ISO | `vm` | `iso: local:iso/FILE.iso` | Creates an empty VM, the ISO in its CD drive | the installer, in the console |
| Container from a file | `ct` | `template: local:vztmpl/FILE.tar.zst` | Creates it from the template file | root password, keys, address, DNS |
| Container clone | `ct` | `template: 920`, a container template | Clones it (linked, or full) | address, DNS; keys and password of the template |

## Example

One file with the four kinds, on two clusters:

```yaml title="lab.yaml"
x-installer: &installer # (1)!
  storage: local-lvm
  disk_size: 32G
  power_on: false

defaults:
  profile: prod # (2)!
  gw: 10.0.0.1
  dns: 10.0.0.1
  ssh_key: ~/.ssh/id_ed25519.pub # (3)!
  vm: # (4)!
    template: 9000
  ct:
    template: local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst
    node: pve1 # (5)!
    storage: local-lvm

guests:
  - type: vm # (6)!
    name: web-{n:02} # (7)!
    count: 2
    user: debian
    ip: 10.0.0.21/24 # (8)!
    cores: 2
    memory: 2G
    disk_size: 20G # (9)!

  - type: vm # (10)!
    name: build-01
    iso: local:iso/debian-13.8.0-amd64-netinst.iso
    cpu: host
    <<: *installer # (11)!

  - type: vm
    name: win-01
    iso: local:iso/windows-server-2025.iso
    virtio_iso: local:iso/virtio-win-0.1.285.iso
    os_type: windows
    memory: 4G
    <<: *installer
    disk_size: 64G # (12)!

  - type: ct # (13)!
    name: dns-01
    ip: 10.0.0.53/24
    memory: 512M
    swap: 0
    password_env: CT_ROOT_PASSWORD # (14)!

  - type: ct # (15)!
    name: proxy-01
    template: 920
    storage: null # (16)!
    ssh_key: null
    ip: dhcp

  - type: vm
    name: web-staging
    profile: staging # (17)!
    user: debian
    ip: 10.1.0.21/24
    gw: 10.1.0.1
```

1. A block for anchors: pvecli ignores `x-` keys. `<<: *installer` merges it into a guest.
2. Every guest goes to `prod` unless it sets its own `profile`.
3. A file path or the key itself. The VM clones and the container from a file take it; the ISO VMs leave it out, since it comes from the common defaults.
4. `defaults.vm` applies to the VMs only, `defaults.ct` to the containers only. Both override the common defaults.
5. A file template sits on the storage of one node: `node` is needed when more than one node is online.
6. A VM clone of template 9000, from `defaults.vm`.
7. A series: `web-01` and `web-02`. `{n}` is the number, `{n:02}` pads it to two digits.
8. The first guest of the series gets this address, the next one `10.0.0.22/24`.
9. The disk of the clone grows to 20 GiB. A size below the template's disk is refused.
10. `iso` on the guest wins over the `template` of `defaults.vm`: this VM is installed, not cloned.
11. Every key of the block, as if written here: `storage`, `disk_size`, `power_on`.
12. A key written after `<<` replaces the one of the block.
13. A container from the file template of `defaults.ct`.
14. The name of an environment variable. The file never holds a password.
15. A container clone: `template` on the guest replaces the file template of `defaults.ct`.
16. `null` clears a default for this guest: `storage` would make a full clone, and a clone takes no `ssh_key`.
17. Another cluster in the same run: the plan is grouped by cluster.

## Keys

`yes`: the key applies to that kind of guest. Blank: it is refused, except `full` on a container from a file, which has no effect. Every key except `name`, `count`, `start` and `id` can also go in `defaults`, `defaults.vm` or `defaults.ct`.

### All guests

| Key | Type | Default | Description |
|:--|:--|:--|:--|
| `type` | `vm` or `ct` | required | Kind of guest. |
| `name` | string | required | VM name or container hostname: letters, digits, dots and dashes. Holds `{n}` in a series. |
| `count` | integer | `1` | Number of guests in the series. Above 1, `name` must contain `{n}`. |
| `start` | integer | `1` | First number of the series. |
| `id` | integer | next free ID | VMID of the guest. In a series, the first ID; the next guests take the following ones. |
| `profile` | string | see [Which profile](#which-profile-a-guest-uses) | Profile of the cluster the guest goes to. |
| `node` | string | the template's node | Node of the new guest. A template on node-local storage is cloned on its own node only. A container from a file or a VM from an ISO needs it when more than one node is online. |
| `cores` | integer | the template's, `2` from an ISO | vCPUs. |
| `memory` | size | the template's, `2G` from an ISO | `2G`, `512M`. A bare number is in MiB. |
| `tags` | list or comma-separated string |  | Tags of the guest. |
| `pool` | string |  | Resource pool. Must exist. |
| `bridge` | string | the template's, `vmbr0` for a new card | Bridge of the first network card. A clone keeps its MAC address and model. |
| `vlan` | integer |  | VLAN tag of the first network card, 1 to 4094. |
| `power_on` | boolean | `true` | Start the guest once it is created. |

### Source and disk

| Key | VM clone | VM ISO | CT file | CT clone | Description |
|:--|:-:|:-:|:-:|:-:|:--|
| `template` | yes | | yes | yes | VM clone: ID of a template VM. Container: a file template (`local:vztmpl/...`) or the ID of a container template. |
| `iso` | | yes | | | The installer, `STORAGE:iso/FILE.iso`. |
| `storage` | yes | yes | yes | yes | A clone: storage of a full clone (implies `full`). ISO: storage of the disk, required. File: storage of the root disk (Proxmox VE's default is `local`). |
| `full` | yes | | | yes | Full clone instead of a linked clone, `false` by default. A template on a storage without linked clones is always cloned in full. |
| `disk_size` | yes | yes | yes | yes | A clone: grows the boot disk to this size, never below the template's. ISO (required) and file: size of the new disk, whole GiB. A bare number is in GiB. |

### Inside the guest

| Key | VM clone | VM ISO | CT file | CT clone | Description |
|:--|:-:|:-:|:-:|:-:|:--|
| `ip` | yes | | yes | yes | IPv4 with prefix, or `dhcp`. Containers also take `manual`. Steps by one in a series. |
| `gw` | yes | | yes | yes | IPv4 gateway. |
| `ip6` | yes | | yes | yes | IPv6 with prefix, `dhcp` or `auto`. Containers also take `manual`. Steps by one in a series. |
| `gw6` | yes | | yes | yes | IPv6 gateway. |
| `dns` | yes | | yes | yes | DNS servers, IP addresses separated by spaces. |
| `search_domain` | yes | | yes | yes | DNS search domain. |
| `ssh_key` | yes | | yes | | Public keys: a file path or the key itself, or a list. |
| `password_env` | yes | | yes | | Environment variable holding the password: of the cloud-init user for a VM, of root for a container (5 characters or more). |
| `user` | yes | | | | User created by cloud-init. |
| `upgrade` | yes | | | | Upgrade the packages at first boot. |

A VM clone gets a cloud-init drive when its template has none.

### Hardware

| Key | VM clone | VM ISO | CT file | CT clone | Description |
|:--|:-:|:-:|:-:|:-:|:--|
| `cpu` | yes | yes | | | CPU type, `host` for nested virtualization. A clone keeps its template's unless set; `x86-64-v2-AES` from an ISO. |
| `os_type` | | yes | | | `linux` (default), `windows` (Windows 11, Server 2022/2025), or a type of Proxmox VE (`l26`, `win10`, `other`...). |
| `virtio_iso` | | yes | | | Windows: the VirtIO drivers ISO, in a second CD drive. |
| `swap` | | | yes | yes | Swap. `0` means none. |
| `unprivileged` | | | yes | yes | `true` by default at creation. A clone keeps its template's: another value is an error. |
| `features` | | | yes | yes | `{nesting: 1}` or `nesting=1,keyctl=1`. Known: `force_rw_sys`, `fuse`, `keyctl`, `mknod`, `mount`, `nesting`. |

A key left out keeps the template's value or Proxmox VE's default.

## A VM installed from an ISO

With `iso`, pvecli builds the VM itself:

| | |
|:--|:--|
| Disk | `scsi0`, empty, `disk_size` on `storage` (qcow2 on a file storage) |
| CD drives | `ide2` with the ISO; `ide0` with `virtio_iso` |
| Boot order | disk, CD, network: the empty disk is skipped on the first start, the installed system boots after |
| Network | `net0`, VirtIO, on `bridge` with `vlan` |
| Windows | `os_type: windows`: UEFI with secure boot, a TPM 2.0 and the q35 machine, their small disks on `storage` |

A VM is cloned or installed, never both:

- `template` and `iso` on one guest are an error. When one comes from the defaults and the other from the guest, the guest's wins.
- An ISO install has no cloud-init. The cloud-init keys (`user`, `upgrade`, `ssh_key`, `password_env`, `ip`, `gw`, `ip6`, `gw6`, `dns`, `search_domain`) are refused on the guest and in `defaults.vm`; the guest clears one of `defaults.vm` with `null`. From the common `defaults`, which also feed the clones and the containers, they are left out without an error.

## Defaults

A guest takes, for each key, the first value set among: the guest, `defaults.vm` or `defaults.ct` for its type, then `defaults`. A key of `defaults.vm` never reaches a container, nor one of `defaults.ct` a VM. A key in `defaults` that one type does not take is left out for that type.

### Which profile a guest uses

The first one set among:

1. `profile` on the guest,
2. `profile` in `defaults` (or `defaults.vm`, `defaults.ct`),
3. `-p` on the command line,
4. `default_profile` of the [configuration file](config-file.md).

`-p` takes a single profile here. To deploy on several clusters in one run, set `profile` on the guests.

### Clearing a default

`null` sets a key back to no value for one guest, as `storage: null` and `ssh_key: null` on `proxy-01` above. `type`, `name`, `count`, `start`, `template`, `iso`, `full` and `power_on` cannot be `null`.

## YAML anchors

The file is read as YAML, anchors included: `&name` names a value or a block, `*name` repeats it, and `<<: *name` merges the keys of a block into a guest, the keys written next to it winning. A guest gets exactly the keys it would have if they were written in it, and the same checks apply.

```yaml
x-installer: &installer
  iso: local:iso/debian-13.8.0-amd64-netinst.iso
  storage: local-lvm
  disk_size: 16G
x-web-tags: &web-tags [web, prod]
guests:
  - {type: vm, name: build-01, <<: *installer}
  - {type: vm, name: build-02, <<: *installer, disk_size: 32G}
  - {type: vm, name: web-01, template: 9000, tags: *web-tags}
```

## What the check refuses

The whole file is checked on every cluster before anything is created, and every problem is listed with the entry it comes from. Besides wrong types and unknown keys, the check refuses:

- a `password` key: use `password_env`,
- a key that does not apply to the kind of guest (see the tables above),
- `template` and `iso` on one VM, or a cloud-init key on a VM installed from an ISO,
- two guests with the same name or the same `id` on one cluster, or the same static address twice in the file,
- a series whose addresses leave their subnet,
- a template, ISO, node, storage, bridge or pool the cluster does not have, a template that is not one, or an `id` already taken.

A guest whose name already exists on its cluster is shown as `present` and skipped. An existing guest of the other type with that name is a conflict.

```text
Error: guests[0] (web-{n:02}): never put a password in the file: use
password_env: NAME_OF_AN_ENV_VAR
Error: guests[0] (web-{n:02}): the series from 10.0.0.253/24 leaves 10.0.0.0/24
at its guest number 3
Error: guests[1] (inst-01): an ISO install has no cloud-init: ssh_key, ip cannot
apply
Error: guests[2] (dns-01): key 'user' does not apply to a ct
Error: 4 problem(s) in the file: nothing was created
```
