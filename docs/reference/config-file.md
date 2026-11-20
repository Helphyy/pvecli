---
icon: lucide/file-cog
title: Configuration file
description: Where pvecli keeps its profiles, every field of a profile, profile groups and how secrets are encrypted.
---

# Configuration file

pvecli reads its profiles from `~/.config/pvecli/config.yaml`. The path is fixed. [config add](../cli/config/add.md) creates the file, [config edit](../cli/config/edit.md) and [config remove](../cli/config/remove.md) change it, and you can edit it by hand for the fields these commands do not cover.

## Example

Two profiles and one group. pvecli writes the keys in alphabetical order; they are grouped here for reading, and the `output` block is left out.

```yaml title="~/.config/pvecli/config.yaml"
default_profile: prod # (1)!
groups:
  all: # (2)!
  - prod
  - staging
profiles:
  prod:
    host: 10.0.0.11 # (3)!
    port: 8006
    verify_ssl: false # (4)!
    timeout: 30
    auth:
      type: token
      user: pvecli@pve
      token_name: automation # (5)!
      token_value: AGE:YWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+IFgyNTUxOSB3Z2... # (6)!
    ssh_port: 22
    rdp_port: 3389
    sdn_write: false # (7)!
    fleet: false # (8)!
  staging:
    host: 10.1.0.11
    port: 8006
    verify_ssl: true
    timeout: 30
    auth:
      type: password
      user: root@pam
      password: AGE:YWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+IFgyNTUxOSA5WHBK... # (9)!
    ssh_user: admin # (10)!
    ssh_port: 22
    rdp_port: 3389
    sdn_write: true
    fleet: true
```

1. The profile used when a command gets no `-p`.
2. Used as `-p @all`. A group skips its members marked `fleet: false`.
3. One node of the cluster, without `https://` or port. pvecli reaches the whole cluster through it.
4. A Proxmox VE node has a self-signed certificate until you install another one.
5. The token ID only, the part after `!` in `pvecli@pve!automation`.
6. Encrypted by pvecli. You can write the secret in clear: pvecli encrypts it the next time it reads the file.
7. The SDN commands that write refuse to run on `prod`.
8. `prod` is left out of `-p @all` and `-p '*'`, and is reached only when named.
9. A password profile logs in with a ticket. [node vnc](../cli/node/vnc.md) needs one.
10. Only written when it is not `root`, the default.

## Top-level keys

| Key | Type | Default | Description |
|:--|:--|:--|:--|
| `default_profile` | string | the first profile added | Profile used when a command gets no `-p`. Set it with [config default](../cli/config/default.md). |
| `profiles` | mapping | | One entry per profile, keyed by its name. |
| `groups` | mapping of lists | `{}` | Named sets of profiles, used as `-p @name`. |
| `output` | mapping | | Written by pvecli with `format`, `colors` and `confirm_destructive`. This version does not read it. |

The forms `-p` accepts (one name, a list, a pattern, a group) are described in [Run a command on several clusters](../fleet/several-clusters.md).

## Profile fields

| Field | Type | Default | Description |
|:--|:--|:--|:--|
| `host` | string | required | IP address or DNS name of a node, without scheme or port. |
| `port` | integer | `8006` | Port of the Proxmox VE API. |
| `verify_ssl` | boolean | `true` | Check the node's TLS certificate. `config add` writes `false` unless you pass `--verify-ssl` or answer yes. |
| `timeout` | integer | `30` | Timeout of each API request, in seconds. |
| `auth` | mapping | required | How pvecli logs in: see below. |
| `ssh_user` | string | `root` | SSH user of `node ssh`, `vm ssh` and `ct ssh`, and of the node used as jump host by `--jump` (`vm ssh`, `ct ssh`, `vm rdp`). |
| `ssh_port` | integer | `22` | SSH port of `node ssh`, `vm ssh`, `ct ssh` and of the `vm rdp --jump` tunnel. |
| `ssh_key` | string | | Private key passed to `ssh -i` by the same commands. Not set by any command: edit the file. |
| `rdp_user` | string | | User of `vm rdp`. When unset, pvecli passes no user to the RDP client. `config add` and `config edit` show `Administrator` for an unset value, and store an answer of `Administrator` as unset. |
| `rdp_port` | integer | `3389` | RDP port of the guests. |
| `sdn_write` | boolean | `false` | Allows the SDN commands that write on this cluster. See [Manage SDN zones and vnets](../infrastructure/sdn.md). |
| `fleet` | boolean | `true` | `false` keeps the profile out of patterns and groups. |

The options `-u`, `-P` and `-i` of the SSH commands take precedence over `ssh_user`, `ssh_port` and `ssh_key`; `-u` and `-P` of `vm rdp` over `rdp_user` and `rdp_port`.

pvecli keeps the keys it does not know in a profile and at the top level: a field written by a newer version survives an edit by an older one. Unknown keys inside `auth` are dropped the next time pvecli writes the file.

### Keys of `auth`

| Key | Type | Description |
|:--|:--|:--|
| `type` | `token` or `password` | Required. |
| `user` | string | Required. User with its realm: `root@pam`, `pvecli@pve`. |
| `token_name` | string | Token ID, required with `type: token`. |
| `token_value` | string | Token secret, required with `type: token`. Encrypted. |
| `password` | string | Password, required with `type: password`. Encrypted. |

## Fields the commands do not edit

`config edit` changes the name, host, port, user, authentication, `verify_ssl`, `ssh_user`, `ssh_port`, `rdp_user` and `rdp_port`, and keeps every other field as it is. Edit the file by hand for `timeout`, `ssh_key`, `sdn_write`, `fleet` and `groups`.

Renaming a profile with `config edit` also renames it in `default_profile` and in the groups. Removing the default profile with `config remove` makes the first remaining profile the default. Removing a profile does not remove it from the groups: a group that still lists it fails with `Group 'all' lists unknown profile(s): ...` until you delete the name from the group.

## Secrets

`auth.password` and `auth.token_value` are encrypted with [age](https://age-encryption.org). The key is generated on first use in `~/.config/pvecli/.age-identity`. An encrypted value starts with `AGE:`.

A secret written in clear in the file is encrypted the next time pvecli reads it. This is how you change a token by hand: replace the `AGE:` value with the new secret and run any command.

!!! warning
    The encrypted values can only be read with the `.age-identity` file that encrypted them. Copy both files together to move your profiles to another machine. If the key is lost, pvecli stops with:

    ```text
    Error: Failed to load config: No matching keys found
    ```

    Write each `token_value` and `password` again in clear: pvecli encrypts them with a new key.

## Files in the folder

| Path | Written by | Content |
|:--|:--|:--|
| `~/.config/pvecli/config.yaml` | `config` commands | Profiles, groups, default profile. Mode `0600`, written to a temporary file then renamed. |
| `~/.config/pvecli/.age-identity` | first use | Key of the encrypted secrets. Mode `0600`. |
| `~/.config/pvecli/maintenance/` | `node maintenance enter` | One file per node in maintenance, read by `node maintenance exit`. |
| `~/.config/pvecli/tag_color.yml` | the [tag color](../cli/tag/color.md) commands | Colors offered when you color a tag. |

The folder is created with mode `0700`.
