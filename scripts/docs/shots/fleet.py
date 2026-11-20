"""Captures of the Fleet section (docs/fleet/).

Setup for the manual shots (VMIDs 860-869, removed when done):
  - prod: container 860 proxy-02 on pve1, rootfs on ceph-vm, running,
    net0 static 10.0.0.86/24, notes "Reverse proxy for www.example.com".
  - staging: VM 861 web-02 on pve6, stopped, scsi0 on local-lvm,
    ipconfig0 10.1.0.87/24, snapshot before-upgrade.
"""

from scripts.docs.screens import Shot

SHOTS = [
    # Run a command on several clusters
    Shot("fleet-vm-list", ["vm", "list", "-p", "{profile},staging"],
         "One header per profile, each cluster's own table", width=100, manual=True),
    Shot("fleet-node-list-all", ["node", "list", "-p", "*"],
         "A pattern: every profile, prod then staging"),
    Shot("fleet-find-json-all", ["find", "10.0.0.86", "-p", "*", "--json"],
         "--json on several profiles: one document keyed by profile", manual=True),
    Shot("fleet-failed-profile", ["snapshot", "list", "--vms", "861", "-p", "*"],
         "A profile fails, the others still run, summary line at the end",
         marks=["Error: No guest matches these filters", "Failed on 1/2 profile(s): prod"],
         manual=True),

    # Find a guest, an address or a disk
    Shot("fleet-find-ip", ["find", "10.0.0.86", "-p", "*"],
         "An IP on every cluster: config and live address of the container",
         marks=["config", "lxc"], manual=True),
    Shot("fleet-find-network", ["find", "10.0.0.0/24", "-p", "{profile}"],
         "A network: guests, nodes and storage servers in it", width=100, manual=True),
    Shot("fleet-find-tasks", ["find", "860", "--tasks", "-p", "{profile}"],
         "--tasks: who did what last on the guest", width=100, manual=True),
    Shot("fleet-find-volume", ["find", "vm-861-disk-0", "-p", "staging"],
         "A disk volume: the guest and the disk key that uses it", manual=True),
    Shot("fleet-find-orphan", ["find", "vm-869-disk-0", "-p", "{profile}"],
         "A volume no guest uses"),
    Shot("fleet-find-text", ["find", "reverse", "-p", "{profile}"],
         "Text: matched in the notes of a guest", manual=True),
    Shot("fleet-find-filters", ["find", "--on-storage", "local-lvm", "--stopped", "-p", "*"],
         "Filters without a query, on every cluster", manual=True),

    # Audit a cluster
    Shot("fleet-audit", ["audit", "-p", "{profile}"],
         "The full audit of one cluster", manual=True),
    Shot("fleet-audit-problems", ["audit", "-p", "*", "--problems"],
         "Only WARN and FAIL, on every cluster", manual=True),

    # Use pvecli in scripts
    Shot("fleet-audit-json", ["audit", "-p", "staging", "--problems", "--json"],
         "Audit findings as JSON (wide: a long detail must not wrap)", width=100),
    Shot("fleet-snapshot-json", ["snapshot", "list", "-p", "staging", "--json"],
         "snapshot list as JSON, snaptime in epoch seconds", manual=True),
]
