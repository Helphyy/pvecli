<div align="center">

<img src="assets/demo.png" alt="pvecli in a terminal" width="800"/>

**pvecli: Proxmox VE from the command line**

[![Python](https://img.shields.io/badge/python-3.10+-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![License](https://img.shields.io/badge/license-GPLv3-22c55e)](LICENSE.txt)
[![Version](https://img.shields.io/badge/version-1.13.0-6366f1)](https://github.com/Helphyy/pvecli/releases)
[![Proxmox VE](https://img.shields.io/badge/Proxmox%20VE-8%20%7C%209-E57000)](https://helphyy.github.io/pvecli/)

**[Documentation](https://helphyy.github.io/pvecli/)** · [Installation](https://helphyy.github.io/pvecli/start/install/) · [Commands](https://helphyy.github.io/pvecli/cli/) · [Releases](https://github.com/Helphyy/pvecli/releases)

</div>

pvecli is a command-line tool for Proxmox VE. It manages guests, nodes, storage, SDN, Ceph, backups and HA, on one cluster or on several at once. It talks to the Proxmox VE API with an API token: nothing is installed on the nodes.

```bash
pipx install git+https://github.com/Helphyy/pvecli.git
```

Then follow [Connect to Proxmox VE](https://helphyy.github.io/pvecli/start/connect/) to create a token with the right role, and:

```bash
pvecli config add prod
pvecli config test -p prod
pvecli vm list -p prod
```

## What it covers

| Command | What for | Guide |
|:--------|:---------|:------|
| `vm`, `ct` | Create, clone, start, stop, migrate, snapshot, consoles, HA | [Guests](https://helphyy.github.io/pvecli/guests/) |
| `deploy` | VMs and containers from a YAML file, checked first, idempotent | [Deploy from a YAML file](https://helphyy.github.io/pvecli/guests/deploy-yaml/) |
| `cluster`, `node` | Health, create and join, maintenance, reboot or shut down a cluster | [Infrastructure](https://helphyy.github.io/pvecli/infrastructure/) |
| `storage`, `sdn`, `ceph` | Storages and their content, zones and vnets, Ceph status and flags | [Infrastructure](https://helphyy.github.io/pvecli/infrastructure/) |
| `backup` | Coverage, run, restore, prune, jobs, files of a PBS backup | [Backups](https://helphyy.github.io/pvecli/backups/) |
| `find`, `audit` | An IP, MAC or disk on every cluster; one read-only health report | [Fleet](https://helphyy.github.io/pvecli/fleet/) |
| `snapshot`, `tag`, `pool` | Snapshots of many guests, tags and pools | [Guests](https://helphyy.github.io/pvecli/guests/) |
| `config` | One profile per cluster, groups of profiles | [Several clusters](https://helphyy.github.io/pvecli/fleet/several-clusters/) |

A few commands:

```bash
pvecli vm stop 100-105 -p prod                        # a range, after a confirmation
pvecli cluster health -p prod,staging                 # two clusters, one report
pvecli find 10.0.0.31 -p '*'                          # who has this IP, on any cluster
pvecli vm exec 100-103 -p prod -- 'apt update && apt upgrade -y'
pvecli backup status --failed -p '*'                  # failed backups everywhere
pvecli cluster reboot -p prod                         # plan, checks, then node by node
```

## Development

```bash
git clone https://github.com/Helphyy/pvecli.git
cd pvecli
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

```bash
ruff check src tests   # lint
mypy src               # type check
pytest                 # static, unit and functional tests, no network
PVECLI_LAB=1 PVECLI_LAB_PROFILES=lab1 pytest -m lab   # read-only, against a lab
```

The functional tests run the real CLI against a fake Proxmox API fed with responses recorded on real clusters: see [tests/README.md](tests/README.md). CI runs them on Python 3.10 to 3.14.

Documentation: `pip install -e ".[docs]"`, then `zensical serve` for a live preview. After changing a command or its help, run `python -m scripts.docs.gen_reference` (CI runs it with `--check`). Writing rules and terminal captures: [scripts/docs/AUTHORING.md](scripts/docs/AUTHORING.md).

## License

GPL-3.0-or-later, see [LICENSE.txt](LICENSE.txt).

pvecli is an independent project, not affiliated with or endorsed by Proxmox Server Solutions GmbH. Proxmox and Proxmox VE are trademarks of Proxmox Server Solutions GmbH.
