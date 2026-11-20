"""Captures of the Infrastructure section (docs/infrastructure/).

Shots marked manual need the test guests of the section (VMIDs 840-849 on
the prod lab: a VM and two containers running on pve4, in pool web) or
change the lab for a moment: run them by name.
"""

from scripts.docs.casts import DOWN, ENTER, SPACE, Cast
from scripts.docs.screens import Shot

P = ["-p", "{profile}"]
STAGING = ["-p", "staging"]
NO = [("(n): ", "n")]

SHOTS = [
    # health.md
    Shot("infrastructure-health", ["cluster", "health", *P],
         "Quorum, votes, HA and Ceph; Ceph warns (slow BlueStore ops under load)",
         manual=True, marks=["Failure tolerance", "HEALTH_WARN"]),
    Shot("infrastructure-status", ["cluster", "status", *P],
         "Cluster entry and one line per node"),
    Shot("infrastructure-tasks", ["cluster", "tasks", "-l", "8", *P],
         "Last tasks, one of them failed", manual=True, width=100,
         marks=["start failed"]),
    Shot("infrastructure-node-list", ["node", "list", *P],
         "Nodes with CPU, memory, disk and uptime", width=100),

    # usage.md
    Shot("infrastructure-usage", ["cluster", "usage", *P],
         "Hypervisor load, guest workload, overhead, pools and storage",
         manual=True, width=100,
         marks=["Hypervisor load", "Guest workload", "Overhead", "Provisioned"]),
    Shot("infrastructure-storage-list", ["storage", "list", "-n", "pve1", *P],
         "Storage seen from one node", width=100),
    Shot("infrastructure-node-info", ["node", "info", "pve4", *P],
         "One node: version, CPU, memory, root FS, swap"),
    Shot("infrastructure-pool-usage", ["pool", "usage", "web", *P],
         "Usage of one resource pool", manual=True, width=100),

    # node-maintenance.md
    Shot("infrastructure-updates", ["node", "updates", *P],
         "Pending updates per node, from the last apt refresh"),
    Shot("infrastructure-evacuate", ["node", "evacuate", "pve4", "pve5", *P],
         "Plan of an evacuation, answered n", stdin="n\n",
         echo=NO, manual=True),
    Shot("infrastructure-maintenance-enter", ["node", "maintenance", "enter", "pve4", *P],
         "Plan of a maintenance, answered n", stdin="n\n",
         echo=NO, manual=True,
         marks=["Guests to move:", "set noout"]),
    Shot("infrastructure-node-reboot", ["node", "reboot", "pve4", *P],
         "Reboot of one node, answered n", stdin="n\n", echo=NO, width=100),

    # cluster-power.md
    Shot("infrastructure-preflight", ["cluster", "preflight", *P],
         "Preflight of a 5-node cluster", width=100),
    Shot("infrastructure-shutdown-plan", ["cluster", "shutdown", *P],
         "Plan of a cluster shutdown, answered n", stdin="n\n",
         echo=NO, manual=True,
         marks=["connected (last)"]),
    Shot("infrastructure-ceph-flags", ["ceph", "flags", *P],
         "Ceph OSD flags and their meaning", width=100),
    Shot("infrastructure-ceph-unset", ["ceph", "unset", "noout,nobackfill,norecover,norebalance", *P],
         "Clearing the flags a shutdown sets (writes: run by name)", manual=True),

    # create-join.md
    Shot("infrastructure-preflight-standalone", ["cluster", "preflight", *STAGING],
         "Preflight of a standalone node", width=100,
         marks=["standalone node, ready to create or join"]),
    Shot("infrastructure-create", ["cluster", "create", "staging", *STAGING],
         "Plan of a cluster creation, confirmation left empty", stdin="\n",
         echo=[("to confirm: ", "")]),

    # storage.md
    Shot("infrastructure-storage-unreachable",
         ["storage", "add", "nfs-iso", "-t", "nfs", "--server", "10.0.0.20",
          "--export", "/srv/iso", *P],
         "The discovery from every node fails: nothing is created", width=100),
    Shot("infrastructure-storage-info", ["storage", "info", "pve1", "ceph-vm", *P],
         "One storage seen from one node"),
    Shot("infrastructure-content-download",
         ["storage", "content", "download", "pve1", "local", "-y",
          "-u", "https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/x86_64/alpine-virt-3.22.8-x86_64.iso",
          "-c", "93ccf2243ee4fc19c78063774f92ff3d328390bae76bda5bfeaf42db46ce5059", *P],
         "ISO downloaded by the node, checksum checked", manual=True, width=100),
    Shot("infrastructure-content-list", ["storage", "content", "list", "pve1", "local", "-t", "iso", *P],
         "ISO images of a storage", width=100),
    Shot("infrastructure-content-remove",
         ["storage", "content", "remove", "pve1", "local",
          "local:iso/alpine-virt-3.22.8-x86_64.iso", "-y", *P],
         "The ISO removed", manual=True, width=100),
    Shot("infrastructure-storage-remove", ["storage", "remove", "pbs", *P],
         "Removing a definition, answered n", stdin="n\n", echo=NO),

    # sdn.md
    Shot("infrastructure-sdn-zones", ["sdn", "zones", *P],
         "Zones and the nodes they are deployed on"),
    Shot("infrastructure-sdn-list", ["sdn", "list", *P],
         "Vnets with their zone and node coverage"),
    Shot("infrastructure-sdn-health", ["sdn", "health", *P],
         "SDN health of an applied configuration"),
    Shot("infrastructure-sdn-refused", ["sdn", "vnet", "add", "vnet9", "-z", "zvlan", *STAGING],
         "A profile without sdn_write refuses SDN writes"),

    # ceph.md
    Shot("infrastructure-ceph-status", ["ceph", "status", *P],
         "Ceph health with the check behind a HEALTH_WARN", manual=True,
         marks=["BLUESTORE_SLOW_OP_ALERT"]),
    Shot("infrastructure-ceph-osd", ["ceph", "osd", *P], "OSD tree"),
    Shot("infrastructure-ceph-pool", ["ceph", "pool", *P], "Ceph pools"),
    Shot("infrastructure-ceph-versions", ["ceph", "versions", *P],
         "Version of every Ceph daemon"),
    Shot("infrastructure-ceph-log", ["ceph", "log", "-l", "4", *P],
         "Tail of the Ceph log", width=100),
]

CASTS = [
    # storage.md: the content checklist of storage config; snippets ticked,
    # then the change refused at the last question (nothing is written)
    Cast("infrastructure-storage-config", ["storage", "config", "pve1", "local", *P],
         [(3.0, DOWN), (0.5, DOWN), (0.5, DOWN), (0.5, DOWN), (0.5, DOWN),
          (0.8, SPACE), (1.2, ENTER), (2.0, "n"), (0.6, ENTER), (1.5, "")],
         rows=24),
]
