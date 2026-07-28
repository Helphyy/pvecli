# Command Reference

Every command supports `--profile, -p` to target a specific cluster profile.
Most commands work **interactively** when called without arguments.

Multi-target commands accept single IDs, comma-separated lists, or ranges:
`100`, `100,101,102`, `100-105`, `100-103,200,300-302`

List commands (`vm`, `ct`, `node`, `storage`, `pool`, `tag`) accept `--order/-o COLUMN` to sort by a column (case-insensitive, prefix match). The sorted column is moved to the first position:

```bash
pvecli vm list -o pool         # Sort VMs by pool, Pool column first
pvecli vm list -o mem          # Prefix match: sorts by Memory usage
pvecli node list -o cpu
pvecli storage list -o usage
```

`vm list` and `ct list` also display a **Pool** column.

`cluster usage`, `cluster resources`, `pool usage`, `pool list`, `pool info`, `node list` and `storage list` accept `--json` for machine-readable output: raw byte values, no colors, no tables, errors on stderr.

Sizes are displayed in **base 1000** (KB, MB, GB, TB), the same units as the Proxmox web interface, so a value shown by pvecli always matches the one shown by the GUI.

---

## config

```
pvecli config add [NAME]           Add a new profile (interactive wizard)
pvecli config edit [NAME]          Edit an existing profile
pvecli config remove [NAME]        Remove a profile (--all for all)
pvecli config default [NAME]       Set the default profile
pvecli config list                 List all profiles
pvecli config info [NAME]          Show profile details (--all for all)
pvecli config test                 Test connection to Proxmox
pvecli config login                Open Proxmox web UI in browser
```

---

## node

```
pvecli node shutdown [NODE]        Shutdown a node (stops all guests, then powers off)
pvecli node reboot [NODE]          Reboot a node (stops all guests, then reboots)
pvecli node vnc [NODES]            Open VNC shell(s) as browser tabs (--split for windows)
pvecli node ssh [NODE]             SSH into a node (--user, --port, --key, --command)
pvecli node list                   List all cluster nodes
pvecli node info [NODE]            Show node details (--all for all)
```

Node shutdown/reboot auto-detects if you are connected to the target node and warns before losing CLI access.

---

## vm

```
pvecli vm start [VMIDS]            Start VMs
pvecli vm stop [VMIDS]             Hard stop VMs (--yes, --timeout)
pvecli vm shutdown [VMIDS]         Graceful shutdown (--timeout, --force)
pvecli vm reboot [VMIDS]           Reboot VMs (--timeout)
pvecli vm suspend [VMIDS]          Suspend VMs
pvecli vm resume [VMIDS]           Resume suspended VMs
pvecli vm lock [VMID] [TYPE]       Lock a VM (requires root@pam)
pvecli vm unlock [VMID]            Unlock a VM (requires root@pam)
pvecli vm add [NODE]               Create a new VM (interactive wizard or CLI options)
pvecli vm clone [VMID]             Clone a VM
pvecli vm edit [VMID]              Edit VM configuration interactively
pvecli vm remove [VMIDS]           Delete VMs (--purge, --force)
pvecli vm template [VMIDS]         Convert VMs to templates
pvecli vm vnc [VMIDS]              Open VNC console(s) as browser tabs (--split for windows)
pvecli vm ssh [VMID]               SSH into VM (--jump for jump host)
pvecli vm rdp [VMID]               Open RDP session
pvecli vm exec [VMIDS] -- CMD      Execute command via QEMU Guest Agent
pvecli vm list                     List all VMs (--node, --status)
pvecli vm info [VMID]              Show detailed VM info
```

### vm exec

Commands are passed after `--` separator. The shell is auto-detected from the VM's OS type (`sh` on Linux, `cmd` on Windows).

```bash
pvecli vm exec 102 -- id
pvecli vm exec 102 -- 'apt update && apt upgrade -y'
pvecli vm exec 100-103 -- systemctl restart chrony
pvecli vm exec 106 -s powershell -- Get-Service
pvecli vm exec 102 -q -- apt install chrony -y    # --quiet suppresses stderr
```

Options: `--timeout/-t` (default 30), `--quiet/-q`, `--shell/-s` (sh/bash/cmd/powershell)

### vm tag

```
pvecli vm tag list [VMID]          List tags on a VM
pvecli vm tag add [VMID] [TAGS]    Add tags (--replace to overwrite all)
pvecli vm tag remove [VMID] [TAGS] Remove specific tags
```

### vm snapshot

```
pvecli vm snapshot list [VMID]              List snapshots
pvecli vm snapshot add [VMID] [NAME]        Create a snapshot (--description)
pvecli vm snapshot rollback [VMID] [NAME]   Rollback to a snapshot (--reboot)
pvecli vm snapshot remove [VMID] [NAME]     Delete a snapshot
```

### vm ha

Manage VM HA (High Availability) resources. Multi-VMID supported (single, list, range).

```
pvecli vm ha status [VMIDS]        Show HA status (no VMID = list all HA resources)
pvecli vm ha add [VMIDS]           Register VMs as HA resources (wizard if no flags)
pvecli vm ha set [VMIDS]           Modify HA configuration
pvecli vm ha remove [VMIDS]        Remove VMs from HA management (--yes)
```

Common options for `add` / `set`: `--group/-g`, `--state/-s` (started|stopped|enabled|disabled|ignored), `--max-restart/-mr`, `--max-relocate/-mo`, `--comment/-c`.

```bash
pvecli vm ha add 100-105 --group prod --state started
pvecli vm ha set 100,101 --state stopped
pvecli vm ha remove 100 --yes
```

Without flags, `vm ha add` opens an interactive wizard prompting for the HA group, initial state, restart/relocate limits and comment.

---

## ct (LXC containers)

```
pvecli ct start [CTIDS]            Start containers
pvecli ct stop [CTIDS]             Hard stop containers (--yes, --timeout)
pvecli ct shutdown [CTIDS]         Graceful shutdown (--timeout, --force)
pvecli ct reboot [CTIDS]           Reboot containers
pvecli ct add [NODE]               Create a new container (interactive wizard or CLI options)
pvecli ct clone [CTID]             Clone a container
pvecli ct edit [CTID]              Edit container configuration
pvecli ct remove [CTIDS]           Delete containers (--purge, --force)
pvecli ct template [CTIDS]         Convert containers to templates
pvecli ct vnc [CTIDS]              Open VNC console(s) as browser tabs (--split for windows)
pvecli ct ssh [CTID]               SSH into container (--jump)
pvecli ct list                     List all containers (--node, --status)
pvecli ct info [CTID]              Show detailed container info
```

### ct tag

```
pvecli ct tag list [CTID]          List tags on a container
pvecli ct tag add [CTID] [TAGS]    Add tags (--replace to overwrite all)
pvecli ct tag remove [CTID] [TAGS] Remove specific tags
```

### ct snapshot

```
pvecli ct snapshot list [CTID]              List snapshots
pvecli ct snapshot add [CTID] [NAME]        Create a snapshot
pvecli ct snapshot rollback [CTID] [NAME]   Rollback to a snapshot (--reboot)
pvecli ct snapshot remove [CTID] [NAME]     Delete a snapshot
```

### ct ha

Same interface as `vm ha`, applied to LXC containers.

```
pvecli ct ha status [CTIDS]        Show HA status (no CTID = list all HA resources)
pvecli ct ha add [CTIDS]           Register containers as HA resources (wizard if no flags)
pvecli ct ha set [CTIDS]           Modify HA configuration
pvecli ct ha remove [CTIDS]        Remove containers from HA management (--yes)
```

### ct image (LXC image files)

```
pvecli ct image list [NODE] [STORAGE]   List all LXC images in a storage
pvecli ct image add [NODE] [STORAGE]    Download an LXC image from the Proxmox repository
pvecli ct image remove [NODE] [STORAGE] Remove an LXC image from storage
```

---

## storage

```
pvecli storage list                         List all storage (--node)
pvecli storage info [NODE] [STORAGE]        Show storage details & config
pvecli storage config [NODE] [STORAGE]      Edit content types interactively
pvecli storage content list [NODE] [STOR]     List content (--type filter)
pvecli storage content add [NODE] [STOR]      Upload from local file (--source-file, --type)
pvecli storage content download [NODE] [STOR] Download from URL (--url, --filename, --type)
pvecli storage content remove [NODE] [STOR]   Delete content
```

---

## pool

```
pvecli pool add [POOLIDS]                   Create pool(s) (--comment)
pvecli pool remove [POOLID]                 Delete a pool (--force)
pvecli pool content add [POOLID] [VMIDS]    Add VMs / CTs to a pool
pvecli pool content remove [POOLID] [VMIDS] Remove VMs / CTs from a pool
pvecli pool export                          Export all pools to JSON (--output)
pvecli pool import                          Import pools from JSON (--input)
pvecli pool list                            List all resource pools
pvecli pool usage [POOLID]                  Show aggregated CPU / memory / disk usage for a pool
pvecli pool info [POOLID]                   Show pool details
```

---

## cluster

```
pvecli cluster status              Show cluster status
pvecli cluster resources           Show resources (--type vm/ct/node/storage)
pvecli cluster usage               Show node load, guest workload, overhead, pools, storage
pvecli cluster tasks               Show task log (--running, --limit)
pvecli cluster shutdown            Shutdown the entire cluster (orchestrated)
pvecli cluster reboot              Reboot the entire cluster (orchestrated)
```

### cluster usage / pool usage

Aggregates the cluster in two calls, `/cluster/resources` and `/storage` (the
storage configuration, read for the Ceph monitor addresses), plus one content
listing per shared storage holding guest disks, or one per node for a local
one, since each node then holds a capacity of its own. `pool usage` adds one call for the pool
itself. Measured on a Ceph backed cluster, authentication aside: 3 API calls for
`cluster usage`, 2 with `--no-storage`, 4 for `pool usage POOL`, 3 with
`--no-storage`.

```bash
pvecli cluster usage                  # Nodes, guests, overhead, pools, storage
pvecli cluster usage --no-storage     # Two API calls, no provisioned disk
pvecli pool usage PROD            # One pool, guest by guest
pvecli pool usage                     # Interactive pool selection menu
pvecli pool usage --json              # Refused: --json needs an explicit pool ID
pvecli pool usage PROD --json     # Raw bytes, no colors, stdout only
```

Options: `--no-storage`, `--storage NAME`, `--json`, `--profile/-p`

`pool usage` also reports the share of the pool against three denominators: the
capacity installed in the nodes, what the hypervisors really consume right now,
and the whole guest workload. A pool can be 3% of the installed memory, 20% of
the memory actually in use and 40% of the guest workload: any single figure
would hide the other two.

Reported figures and their traps:

- **Hypervisor load** is what the nodes really consume, **guest workload** is the
  sum of the guests. The gap is the **overhead**: Ceph OSDs, PVE services, cache.
- CPU is expressed in **cores**, never in raw added percentages: the PVE `cpu`
  field is a 0..1 fraction of each object's own `maxcpu`.
- Provisioned disk comes from the storage volume listings, never from the guest
  `maxdisk` field, which ignores secondary disks and reads 0 for QEMU guests.
- A shared storage is reported once per node by the API and is merged here, so
  its capacity is counted once.
- PBS datastores are excluded from capacity: they report 0 and hold backups.
- Two RADOS pools count as one capacity only when they agree on both signals:
  the same Ceph monitors, so they belong to one cluster, and the same free
  space, so they rest on one set of disks. Such a group is reported on its
  largest pool as the sum of the used space of its pools plus that common free
  space, and the others are listed with the reason why. Pools of one cluster on
  different device classes (NVMe and HDD) publish different free spaces because
  they use disjoint disks: they keep their own capacity and both are counted.
  When the storage configuration cannot be read, the monitors are unknown, the
  pools are recognised on their free space alone and a warning says so.
- Templates are excluded from every total and reported separately.
- `diskread`, `diskwrite`, `netin` and `netout` are cumulative counters since
  boot, not rates, so they are deliberately not reported.

### cluster shutdown / reboot

Orchestrates a safe cluster-wide power operation:

1. Disables HA resources (prevents migration during shutdown)
2. Sets Ceph maintenance flags if Ceph is detected
3. Stops all guests on each node
4. Shuts down / reboots nodes (connected node always last)

Options: `--skip-ceph`, `--skip-ha`, `--timeout/-t` (default 300), `--yes/-y`

Requires double confirmation (type `SHUTDOWN` or `REBOOT`) unless `--yes` is passed.

---

## tag (global)

```
pvecli tag list                    List all tags across the cluster
pvecli tag add [TAG]               Add / update a tag color (--color)
pvecli tag edit [TAG]              Rename a tag and/or change its color (--name, --color)
pvecli tag remove [TAGS]           Remove tag(s) from all VMs and CTs
pvecli tag export                  Export tags and colors to JSON (--output)
pvecli tag import                  Import tags and colors from JSON (--input)
pvecli tag color init              Initialize the color palette
pvecli tag color list              List palette colors
pvecli tag color add [NAME] [HEX]  Add a color to the palette
pvecli tag color remove [NAME]     Remove a color from the palette
```
