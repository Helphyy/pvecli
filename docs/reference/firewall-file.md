---
icon: lucide/file-lock
title: Firewall file format
description: Every key of the YAML file written by cluster firewall export and read by cluster firewall import.
---

# Firewall file format

[cluster firewall export](../cli/cluster/firewall.md#export) writes this file and [cluster firewall import](../cli/cluster/firewall.md#import) reads it, as YAML or JSON. How to use it is in [Keep the firewall in a file](../fleet/firewall.md).

## Top level

| Key | Type | Description |
|:--|:--|:--|
| `pvecli_firewall` | `1` | Required. |
| `datacenter` | level | The firewall of the datacenter. |
| `nodes` | map of node name to level | The firewall of each node. |
| `guests` | map of VM or container ID to level | The firewall of each guest. |
| `vnets` | map of vnet name to level | The firewall of each SDN vnet (PVE 8.3 or later). |

Any other key is an error. A level the file names becomes exactly what it says: an option left out goes back to its default, an alias, IP set, group or rule left out is removed. A level named with nothing (`890: {}`) is emptied, its options back to their defaults: the firewall off for the datacenter, a guest or a vnet, on for a node. A level not named is not touched.

## Keys of a level

| Key | Datacenter | Node | Guest | Vnet | Description |
|:--|:-:|:-:|:-:|:-:|:--|
| `options` | yes | yes | yes | yes | Map of option to value, as [options](../cli/cluster/firewall.md#options) names them. `yes`/`no`, `true`/`false` and `1`/`0` are all read. |
| `rules` | yes | yes | yes | yes | List of rules, the first one tried first. |
| `aliases` | yes | | yes | | List of aliases. |
| `ipsets` | yes | | yes | | List of IP sets. |
| `groups` | yes | | | | List of security groups. |
| `type` | | | yes | | `vm` or `ct`. Optional: the import stops if the guest is of the other type. |
| `name` | | | yes | | The name of the guest, for the reader. Not checked. |

The options of each level:

| Level | Options |
|:--|:--|
| Datacenter | `enable`, `policy_in`, `policy_out`, `policy_forward`, `ebtables`, `log_ratelimit` |
| Node | `enable`, `log_level_in`, `log_level_out`, `log_level_forward`, `log_nf_conntrack`, `ndp`, `nf_conntrack_allow_invalid`, `nf_conntrack_helpers`, `nf_conntrack_max`, `nf_conntrack_tcp_timeout_established`, `nf_conntrack_tcp_timeout_syn_recv`, `nftables`, `nosmurfs`, `protection_synflood`, `protection_synflood_burst`, `protection_synflood_rate`, `smurf_log_level`, `tcp_flags_log_level`, `tcpflags` |
| Guest | `enable`, `policy_in`, `policy_out`, `dhcp`, `ipfilter`, `macfilter`, `ndp`, `radv`, `log_level_in`, `log_level_out` |
| Vnet | `enable`, `policy_forward`, `log_level_forward` |

## Keys of a rule

| Key | Description |
|:--|:--|
| `type` | Required. `in`, `out`, `forward`, or `group` to insert a security group. |
| `action` | Required. `ACCEPT`, `DROP`, `REJECT`, or the name of the group when `type` is `group`. |
| `macro` | A macro: `SSH`, `HTTPS`, `Ceph`... ([cluster firewall macros](../cli/cluster/firewall.md#macros)). |
| `proto` | Protocol: `tcp`, `udp`, `icmp`... or a number. |
| `source`, `dest` | Address, network, range, alias, or IP set with a `+`. Several, comma separated. |
| `sport`, `dport` | Ports: `443`, `8000:8100`, `22,443`. |
| `icmp-type` | ICMP type, with `proto: icmp`. |
| `iface` | Interface: `net0` on a guest, `vmbr0` on a node. |
| `log` | Log level: `emerg` to `debug`, or `nolog` (the same as no `log`). |
| `enable` | `1` or `0`. Left out: `1`. |
| `comment` | Free text. |

## Keys of the objects

| Object | Keys |
|:--|:--|
| Alias | `name` (required), `cidr` (required): an address or a network, `comment` |
| IP set | `name` (required), `comment`, `entries`: a list of entries |
| Entry of an IP set | `cidr` (required): an address, a network or an alias, `nomatch` (`1`: an exception), `comment` |
| Security group | `name` (required), `comment`, `rules`: a list of rules, as above |

## What the import refuses

Before any change, the import stops when:

- the file does not start with `pvecli_firewall: 1`, or has a key it does not know;
- a node, guest or vnet it names does not exist on the cluster;
- a rule names an alias, IP set or security group that does not exist once the file is applied;
- an object it removes from the datacenter is still used by a level outside the file (unless `--force`);
- the result would close the API to your machine (unless `--force`);
- the profile lacks `firewall_write: true` and the file names the datacenter, a node or a vnet.
