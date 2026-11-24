---
icon: lucide/file-cog
title: Keep the firewall in a file
description: Export the whole firewall of a cluster to YAML, keep it in git, review the plan of an import, and apply the same firewall again or on another cluster.
---

# Keep the firewall in a file

[cluster firewall export](../cli/cluster/firewall.md#export) writes the whole firewall of a cluster to one YAML file: the datacenter with its aliases, IP sets and security groups, then every node, guest and vnet that has something. [cluster firewall import](../cli/cluster/firewall.md#import) makes the cluster match such a file. Between the two, the file can live in git and be reviewed like code.

## Export

--8<-- "screens/fleet-firewall-export.html"

`-ou firewall.yaml` writes it to a file, `--json` as JSON. A node, guest or vnet with no option and no rule is left out. The keys keep the same order from one export to the next, so a diff shows only what changed.

## Write the file

The levels to manage, and nothing else ([firewall.yaml](firewall.yaml)):

```yaml
pvecli_firewall: 1 # (1)!
datacenter: # (2)!
  options:
    enable: 1 # (3)!
  aliases:
  - name: admin-net
    cidr: 10.0.0.0/24
    comment: admins
  ipsets:
  - name: management # (4)!
    entries:
    - cidr: 10.0.0.7
      comment: admin workstation
  groups:
  - name: webserver
    comment: web servers
    rules:
    - type: in
      action: ACCEPT
      macro: HTTP
      enable: 1
    - type: in
      action: ACCEPT
      macro: HTTPS
      enable: 1
guests: # (5)!
  890:
    type: vm
    name: web-01 # (6)!
    options:
      enable: 1
    rules: # (7)!
    - type: group
      action: webserver
      enable: 1
    - type: in
      action: ACCEPT
      macro: SSH
      source: admin-net
      enable: 1
      comment: admins
  891:
    type: ct
    name: dns-01
    options:
      enable: 1
    rules:
    - type: in
      action: ACCEPT
      macro: DNS
      enable: 1
    - type: in
      action: ACCEPT
      macro: SSH
      source: admin-net
      enable: 1
```

1. Required: it marks the file as a pvecli firewall file.
2. A level named in the file becomes exactly what the file says. The aliases, IP sets, groups and rules of the datacenter that the file leaves out are removed.
3. An option left out goes back to its default. Without `enable: 1`, the import turns the datacenter firewall off.
4. Keep your machine in it: the import is checked like any change that can close the access (see [Turn on the cluster firewall](../infrastructure/firewall.md#turn-it-on)).
5. Only guests 890 and 891 are managed. The other guests, and the nodes, are not named: the import does not touch them.
6. `type` and `name` are checked against the cluster: the import stops if 890 is not a VM. The ID is what counts.
7. In the order Proxmox VE tries them, the first one first.

Every key is listed in [Firewall file format](../reference/firewall-file.md).

## Review the plan

The import first prints what it will change, and asks:

--8<-- "screens/fleet-firewall-import-plan.html"

Before the plan, the import checks that every alias, IP set and group a rule names exists once the file is applied, and that the objects it removes from the datacenter are not used by a level outside the file. It lists every problem and stops before any change. `--force` removes objects still used elsewhere.

## Apply it

With `-y` it applies without asking:

--8<-- "screens/fleet-firewall-import-run.html"

The changes go in an order that never leaves a rule pointing to nothing, and never enforces a half-applied firewall:

1. A level the file turns off is turned off first.
2. New and changed aliases, IP sets and groups.
3. The rules: removed ones first, new ones added disabled at their place, then enabled.
4. The objects the file removes.
5. The options, and a firewall the file turns on, last.

If a change fails part way, the import stops, says how many changes were made, and checks the access when the firewall was concerned. Run it again: it starts from what is there now.

Run on a cluster that already matches, it changes nothing:

--8<-- "screens/fleet-firewall-import-again.html"

## Copy a firewall to another cluster

The file names nodes, guests and vnets by name and ID. A level that does not exist on the target cluster stops the import before any change. To copy only the datacenter part (aliases, IP sets, groups, rules of the nodes), export, keep `pvecli_firewall` and `datacenter`, and import on the other cluster:

```bash
pvecli cluster firewall export -ou staging-fw.yaml -p staging
pvecli cluster firewall import staging-fw.yaml -p prod
```

Check the `management` set of the file before: it is the one the target cluster gets.

## What the file does not hold

- `firewall=1` on the network cards of the guests: it is a setting of the guest, see [Filter a guest's traffic](../guests/firewall.md#turn-the-firewall-on).
- The positions of the rules: their order in the list is the position.

The import needs `firewall_write: true` on the profile when the file names the datacenter, a node or a vnet (see [Permissions](../reference/permissions.md#firewall)).
