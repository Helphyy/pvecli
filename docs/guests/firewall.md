---
icon: lucide/brick-wall-shield
title: Filter a guest's traffic
description: Turn on the firewall of a VM or a container, flag its network cards, write its rules, and check that Proxmox VE enforces them.
---

# Filter a guest's traffic

The Proxmox VE firewall filters the traffic of each guest on the node, outside the guest. A rule of a guest applies only when three switches are on:

| Switch | Command | Default |
|:--|:--|:--|
| The firewall of the datacenter | [cluster firewall enable](../cli/cluster/firewall.md#enable) | off |
| The firewall of the guest | [vm firewall enable](../cli/vm/firewall.md#enable) | off |
| `firewall=1` on each network card of the guest | `vm firewall enable --nics` | off, but checked in the web interface when it adds a card |

If one is off, the rules stay written but nothing is filtered. The datacenter switch also turns on the filtering of the nodes themselves: read [Turn on the cluster firewall](../infrastructure/firewall.md) before you turn it on.

## Check what is filtered

[ct firewall status](../cli/ct/firewall.md#status) shows the three switches of a guest, and whether its rules are enforced:

--8<-- "screens/guests-firewall-status-off.html"

`net0 not filtered`: the card has no `firewall=1`. The traffic of that card passes, whatever the rules say.

## Turn the firewall on

[ct firewall enable](../cli/ct/firewall.md#enable) turns the firewall of the container on. `--nics` also sets `firewall=1` on the cards that lack it, in the same change:

--8<-- "screens/guests-firewall-enable-nics.html"

Without `--nics`, pvecli names the cards left unfiltered. A running guest loses its network for a moment while its cards are plugged again: pvecli warns before it asks.

Once the firewall of a guest is on, its input policy applies: `DROP` by default. Everything that comes in is dropped, except what a rule accepts. Write the rules before the datacenter switch is turned on, or the guest becomes unreachable the moment it is. Its output policy is `ACCEPT`.

## Add rules

[vm firewall rule add](../cli/vm/firewall.md#rule-add) adds a rule and shows it, with the rule it lands before, before it is written:

--8<-- "screens/guests-firewall-rule-add.html"

- `-ac` is the action: `ACCEPT`, `DROP`, `REJECT`, or the name of a security group to insert (see [security groups](../infrastructure/firewall.md#share-rules-with-security-groups)).
- `-ma` is a macro: a named service with its protocol and ports. [cluster firewall macros](../cli/cluster/firewall.md#macros) lists them. Without a macro, give `-pt` (protocol) and `-dpt` (port).
- `-src` and `-dst` take an address, a network, an alias, or an IP set with a `+` (`+admins`).
- `-dn` is the direction, `in` by default.

Without `-ac`, pvecli asks for each field in turn.

[vm firewall rule list](../cli/vm/firewall.md#rule-list) shows the rules in the order Proxmox VE tries them. The first rule that matches a packet decides, and position 0 comes first:

--8<-- "screens/guests-firewall-rule-list.html"

An empty source or destination means any. A new rule goes to position 0, as in the web interface: `--pos` puts it elsewhere.

## Change, move and remove rules

| Command | Does |
|:--|:--|
| [rule edit](../cli/vm/firewall.md#rule-edit) `890 1 -src 10.0.0.0/24` | Changes only the fields given. `--clear source` empties one. |
| [rule move](../cli/vm/firewall.md#rule-move) `890 2 0` | Moves rule 2 to position 0. |
| [rule disable](../cli/vm/firewall.md#rule-disable) `890 1`, [rule enable](../cli/vm/firewall.md#rule-enable) | Keeps the rule, without effect while disabled. |
| [rule remove](../cli/vm/firewall.md#rule-remove) `890 1 2` | Removes rules 1 and 2. |

The positions are the ones of `rule list`. Each change shows the rule and asks first. If someone changed the rules of the guest meanwhile, pvecli stops instead of writing over their change.

## Name addresses with aliases and IP sets

An alias names one address or network; an IP set groups several, with exceptions (`--nomatch`). Each guest can have its own, used only in its rules:

```bash
pvecli vm firewall alias add 890 backup-srv 10.0.0.50
pvecli vm firewall ipset add 890 admins 10.0.0.7 10.0.0.8
pvecli vm firewall rule add 890 -ac ACCEPT -ma SSH -src +admins
```

Aliases and IP sets of the datacenter are shared by every guest: see [Turn on the cluster firewall](../infrastructure/firewall.md#name-addresses-for-the-whole-cluster). Renaming one with `--rename` also rewrites the rules and IP sets that name it, which Proxmox VE does not do by itself. pvecli refuses to remove an IP set that a rule still names, unless `--force`.

## Check that it is enforced

Once the datacenter firewall is on, `status` says `Enforced: yes`:

--8<-- "screens/guests-firewall-status-on.html"

[vm firewall options](../cli/vm/firewall.md#options) shows every option of the guest with its default, and [vm firewall set](../cli/vm/firewall.md#set) changes them (`policy_in=REJECT`, `log_level_in=info`, `macfilter=no`...). With a log level on a rule (`-lg info`) or on a policy, [vm firewall log](../cli/vm/firewall.md#log) shows what the firewall matched.

Containers take the same commands under [ct firewall](../cli/ct/firewall.md). To see every guest of the cluster at once, with what is not enforced and why: [cluster firewall status](../cli/cluster/firewall.md#status).
