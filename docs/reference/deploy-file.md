---
icon: lucide/file-text
title: Deploy file format
description: Every key of the YAML file read by pvecli deploy, with its type, its default and what it does.
---

# Deploy file format

[deploy](../cli/deploy.md) reads a YAML file with two top-level keys: `defaults` (optional) and `guests` (a non-empty list). Any other top-level key is an error. How to run the file is in [Deploy guests from a YAML file](../guests/deploy-yaml.md).

## Example

```yaml title="lab.yaml"
defaults:
  profile: prod # (1)!
  gw: 10.0.0.1
  dns: 10.0.0.1
  search_domain: example.com
  ssh_key: ~/.ssh/id_ed25519.pub # (2)!
  tags: [web, deploy]
  vm: # (3)!
    template: 9000
    user: debian
  ct:
    template: local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst # (4)!
    node: pve1 # (5)!
    storage: local-lvm
    features: {nesting: 1}

guests:
  - type: vm
    name: web-{n:02} # (6)!
    count: 2
    ip: 10.0.0.21/24 # (7)!
    cores: 2
    memory: 2G
    disk_size: 20G # (8)!

  - type: vm
    name: db-01
    id: 130
    ip: 10.0.0.30/24
    memory: 4G
    full: true
    storage: local-lvm # (9)!
    power_on: false
    tags: [db] # (10)!

  - type: ct
    name: dns-01
    ip: 10.0.0.53/24
    memory: 512M
    swap: 0
    disk_size: 8G
    password_env: CT_ROOT_PASSWORD # (11)!
```

1. Every guest of the file goes to `prod`, unless it sets its own `profile`.
2. A file path or the key itself. The container gets it because it is created from a file template.
3. `defaults.vm` applies to VMs only, `defaults.ct` to containers only. Both override the common defaults above them.
4. A file template: the container is created, not cloned.
5. Needed for a file template when more than one node is online: the file usually sits on a storage of one node.
6. A series: `web-01` and `web-02`. `{n}` is the number, `{n:02}` pads it to two digits.
7. The first guest of the series gets this address, the next one `10.0.0.22/24`.
8. The boot disk of the clone grows to 20 GiB. A size below the template's disk is refused.
9. A storage implies a full clone: `full: true` only says it again.
10. A key of the guest replaces the default: `db-01` has the tag `db` only.
11. The name of an environment variable. The file never holds a password.

## Keys of a guest

Each entry of `guests` takes the keys below. Every key except `name`, `count`, `start` and `id` can also go in `defaults`, `defaults.vm` or `defaults.ct`.

| Key | Type | Default | Description |
|:--|:--|:--|:--|
| `type` | `vm` or `ct` | required | Kind of guest. |
| `name` | string | required | VM name or container hostname: letters, digits, dots and dashes. Holds `{n}` in a series. |
| `count` | integer | `1` | Number of guests in the series. Above 1, `name` must contain `{n}`. |
| `start` | integer | `1` | First number of the series. |
| `id` | integer | next free ID | VMID of the guest. In a series, the first ID; the next guests take the following ones. |
| `profile` | string | see below | Profile of the cluster the guest goes to. |
| `template` | integer or string | required | VM: ID of a template VM; pvecli adds a cloud-init drive to the clone when the template has none. Container: a file template (`local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst`) or the ID of a container template, which is cloned. |
| `node` | string | the template's node | Node of the new guest. A VM or container template on node-local storage can only be cloned on its own node. For a container from a file template, required unless exactly one node is online. |
| `storage` | string |  | VM: storage of the full clone. Container from a file template: storage of the root disk (Proxmox VE's default is `local`). Container clone: storage of the full clone. Implies a full clone. |
| `full` | boolean | `false` | Full clone instead of a linked clone. A container template on a storage that has no linked clones is always cloned in full. |
| `cores` | integer |  | vCPUs. |
| `memory` | size |  | `2G`, `512M`. A bare number is in MiB. |
| `disk_size` | size |  | VM and container clone: grows the boot disk to this size; a size below the template's disk is an error. Container from a file template: size of the root disk, a whole number of GiB. A bare number is in GiB. |
| `tags` | list or comma-separated string |  | Tags of the guest. |
| `pool` | string |  | Resource pool. Must exist. |
| `ip` | IPv4 with prefix, or `dhcp` |  | Address of the first NIC. Containers also take `manual`. Steps by one in a series. |
| `gw` | IPv4 address |  | Gateway. |
| `ip6` | IPv6 with prefix, `dhcp` or `auto` |  | IPv6 address. Containers also take `manual`. Steps by one in a series. |
| `gw6` | IPv6 address |  | IPv6 gateway. |
| `dns` | string |  | DNS servers, IP addresses separated by spaces. |
| `search_domain` | string |  | DNS search domain. |
| `ssh_key` | string or list |  | Public keys: a file path or the key itself. Containers take keys only from a file template: a clone keeps those of its template. |
| `password_env` | string |  | Environment variable holding the password. VM: the cloud-init user's password. Container: the root password, file templates only, 5 characters or more. |
| `power_on` | boolean | `true` | Start the guest once it is configured. |

VMs only:

| Key | Type | Default | Description |
|:--|:--|:--|:--|
| `user` | string |  | User created by cloud-init. |
| `upgrade` | boolean |  | Upgrade the packages at first boot. |

Containers only:

| Key | Type | Default | Description |
|:--|:--|:--|:--|
| `swap` | size |  | Swap. `0` means no swap. |
| `bridge` | string | `vmbr0` for a new NIC | Bridge of the NIC. |
| `vlan` | integer |  | VLAN tag of the NIC, 1 to 4094. |
| `unprivileged` | boolean | `true` at creation | A clone keeps the value of its template: setting another one is an error. |
| `features` | mapping or string |  | `{nesting: 1}` or `nesting=1,keyctl=1`. Known: `force_rw_sys`, `fuse`, `keyctl`, `mknod`, `mount`, `nesting`. |

A key that does not apply to the guest's type is an error. A key left out keeps the template's value or Proxmox VE's default.

### Which profile a guest uses

The first one set among:

1. `profile` on the guest,
2. `profile` in `defaults` (or `defaults.vm`, `defaults.ct`),
3. `-p` on the command line,
4. `default_profile` of the [configuration file](config-file.md).

`-p` takes a single profile here. To deploy on several clusters in one run, set `profile` on the guests.

### Clearing a default

`null` sets a key back to no value, for one guest:

```yaml
  - type: ct
    name: proxy-01
    template: 920
    storage: null # (1)!
    ssh_key: null # (2)!
```

1. With `storage` from `defaults.ct`, the clone would be a full clone.
2. A container clone takes no keys: without this line the default `ssh_key` is an error.

`type`, `name`, `count`, `start`, `template`, `full` and `power_on` cannot be `null`.

## What the check refuses

The whole file is checked on every cluster before anything is created, and every problem is listed. Besides wrong types and unknown keys, the check refuses:

- a `password` key: use `password_env`,
- two guests with the same name or the same `id` on one cluster,
- the same static address twice in the file,
- a series whose addresses leave their subnet,
- a template, node, storage, bridge or pool the cluster does not have, or an `id` already taken.

A guest whose name already exists on its cluster is shown as `present` and skipped. An existing guest of the other type with that name is a conflict.

Example of a refused file:

```text
Error: guests[0] (web-{n:02}): never put a password in the file: use
password_env: NAME_OF_AN_ENV_VAR
Error: guests[0] (web-{n:02}): the series from 10.0.0.253/24 leaves 10.0.0.0/24
at its guest number 3
Error: guests[1] (dns-01): 'cores' must be an integer, got 'two'
Error: guests[1] (dns-01): key 'user' does not apply to a ct
Error: 4 problem(s) in the file: nothing was created
```
