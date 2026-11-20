---
title: pvecli
template: home.html
hide:
  - navigation
  - toc
description: pvecli is a command-line tool for Proxmox VE 8 and 9 that works over the API, on one cluster or several.
---

# pvecli

pvecli is a command-line tool for Proxmox VE 8 and 9. It talks to the API of one cluster or of several at once, from your workstation; nothing is installed on the nodes.

```bash
pipx install git+https://github.com/Helphyy/pvecli.git
pvecli config add prod
pvecli cluster health -p prod
```

`pvecli config add prod` asks for the address of a node and an API token, and saves them as the profile `prod`. [Connect to a cluster](start/connect.md) shows how to create the token.

--8<-- "screens/home-health.html"

The Ceph line on this lab cluster is a warning: [Check cluster health](infrastructure/health.md) explains each line.

## Where to go next

- [Getting started](start/index.md): install pvecli, connect it to a cluster, first commands.
- [Guests](guests/index.md): create, run, migrate, snapshot and organize VMs and containers.
- [Infrastructure](infrastructure/index.md): health, capacity, nodes, cluster power, storage, SDN and Ceph.
- [Backups](backups/index.md): coverage, backups now and on a schedule, restores, retention.
- [Fleet](fleet/index.md): several clusters at once, find, audit, scripting.
- [Reference](reference/index.md): configuration file, deploy file, permissions, exit codes.
- [All commands](cli/index.md): the full command list, one page per command.

pvecli is free software under the GPL-3.0-or-later, developed on [GitHub](https://github.com/Helphyy/pvecli). It is not affiliated with Proxmox Server Solutions GmbH.
