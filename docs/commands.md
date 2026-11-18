# Command Reference

Every command supports `--profile, -p` to target a specific cluster profile.
Most commands work **interactively** when called without arguments.

Read commands, power commands, `find`, `audit`, `snapshot`, `backup`, `deploy` and `node updates` also accept **several profiles**: `-p lab1,lab4`, `-p 'client-*'`, `-p @prod` (see [configuration](configuration.md#several-profiles-at-once)).

Multi-target commands accept single IDs, comma-separated lists, or ranges:
`100`, `100,101,102`, `100-105`, `100-103,200,300-302`

They also pick guests by **tag, pool or node** instead of IDs (combinable, restricted to the IDs when both are given). The selection is printed first, and commands that change something ask before acting (`-y` to skip):

```bash
pvecli vm exec --tag debian -- apt update
pvecli vm shutdown --pool LAB
pvecli ct start --node pve2 --tag web
pvecli vm ha add --tag critical --nodes pve1,pve2
```

This applies to `vm/ct start, stop, shutdown, reboot`, `vm suspend, resume, exec`, `vm/ct ha status, add, set, remove` and `vm/ct vnc`. `remove` and template conversion stay ID only, on purpose.

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
pvecli node evacuate NODE TARGET   Move every guest off a node before maintenance
pvecli node maintenance enter NODE Empty a node, set Ceph noout, before working on it by hand
pvecli node maintenance exit NODE  Check the cluster, clear noout, bring its guests back
pvecli node maintenance status     Nodes currently in maintenance
pvecli node updates [NODE]         Pending package updates per node (--refresh, --packages)
pvecli node vnc [NODES]            Open VNC shell(s) as browser tabs (--split for windows)
pvecli node ssh [NODE]             SSH into a node (--user, --port, --key, --command)
pvecli node list                   List all cluster nodes
pvecli node info [NODE]            Show node details (--all for all)
```

Node shutdown/reboot auto-detects if you are connected to the target node and warns before losing CLI access.

### node maintenance

```bash
pvecli node maintenance enter pve2          # guests to the node with most free memory
pvecli node maintenance enter pve2 -t pve3  # or to a chosen node
# upgrade / reboot pve2 by hand
pvecli node maintenance exit pve2
```

`enter` checks the quorum, sets the Ceph `noout` flag (OSDs going down trigger no rebalance), evacuates the node and records what it did in `~/.config/pvecli/maintenance/`, per cluster. `exit` waits for the node, checks the quorum, clears `noout` only if it set it and no other node of the cluster is still in maintenance, and moves the guests back. The upgrade itself is never automated.

HA guests: HA would bring them back to their preferred node as soon as it is up again (failback), at every reboot of the maintenance. On clusters using HA rules (PVE 9), `enter` pauses failback on the guests it moves and `exit` restores it, through the API: no root shell on the node is needed. On clusters still using HA groups, failback is a setting of the whole group, so pvecli leaves it alone and prints the `ha-manager crm-command node-maintenance` line to run on the node.

Before changing anything, both commands check the effective privileges of the user (`VM.Migrate` on each guest, `Sys.Modify` on `/` for the Ceph flag, `Sys.Console` on `/` for HA failback) and stop with the list of what is missing. A user without `Sys.Modify` can still run a maintenance with `--no-ceph-flag`.

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
pvecli vm deploy TEMPLATE NAME     Clone a cloud-init template, configure it and start it
pvecli vm cloudinit show VMID      Cloud-init settings, with pending changes
pvecli vm cloudinit set VMID       Change user, keys, IP, DNS; adds the drive if missing
pvecli vm migrate [VMID] [TARGET]  Migrate a VM to another node (live when it runs)
pvecli vm edit [VMID]              Edit VM configuration interactively
pvecli vm remove [VMIDS]           Delete VMs (--purge, --force)
pvecli vm template [VMIDS] [-y]    Convert VMs to templates (asks first: irreversible)
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

Common options for `add` / `set`: `--state/-s` (started|stopped|enabled|disabled|ignored), `--max-restart/-mr`, `--max-relocate/-mo`, `--comment/-c`, and the placement options below.

#### Placement: HA groups or HA rules

PVE 9 replaces HA groups with HA rules, and converts the groups once every node runs 9. pvecli detects which one the cluster uses and refuses the other with a clear message:

- **Clusters still using groups** (PVE 8, or PVE 9 before the conversion): `--group/-g <group>`.
- **Clusters using rules**: `--nodes/-n <node>[:<priority>],...` creates a node-affinity rule named `pvecli-vm<ID>` for the guest, `--strict/--no-strict` restricts it to those nodes instead of preferring them, and `--nodes ''` drops the rule. A guest already placed by a rule pvecli did not create is left alone, since that rule may cover other guests.

```bash
pvecli vm ha add 100 --nodes pve1:2,pve2 --strict      # rules
pvecli vm ha add 100-105 --group prod --state started  # groups
pvecli vm ha set 100 --nodes pve3
pvecli vm ha set 100,101 --state stopped
pvecli vm ha remove 100 --yes                          # also drops it from the rules
```

`vm ha status` shows the group or the rules of each guest, with its live state and node. Without flags, `vm ha add` opens a wizard: the HA group on a groups cluster, the preferred nodes on a rules cluster.

### vm deploy / vm cloudinit

```bash
pvecli vm deploy 9000 web01 --ip dhcp --ssh-key ~/.ssh/id_ed25519.pub --wait-ip 120
pvecli vm deploy 9000 db01 --ip 10.0.0.31/24 --gw 10.0.0.1 --dns 10.0.0.1 \
    --user admin --cores 4 --memory 8G --disk-size 50G --tags db,prod --pool PROD
pvecli vm cloudinit set 120 --ip 10.0.0.22/24 --gw 10.0.0.1   # applied at next reboot
pvecli vm cloudinit show 120
```

`deploy` makes a linked clone (`--full` or `--storage` for a full one), sets CPU, memory, tags and pool, grows the boot disk, writes the cloud-init settings (adding a cloud-init drive if the template has none), starts the VM, and with `--wait-ip` prints its address from the guest agent. Passwords only come from an environment variable (`--password-env`); SSH keys are given as files.

### vm migrate

```bash
pvecli vm migrate 100 pve2                     # live when the VM runs, offline otherwise
pvecli vm migrate 100 pve2 --with-local-disks  # copy local disks during a live migration
pvecli vm migrate 100 pve2 --target-storage ceph-rbd
pvecli vm migrate 100                          # pick the target in a menu
```

The target is checked first: a node missing a storage, local resources or a mapped device is refused with its reason before anything moves. An HA-managed VM moves through the HA manager, and pvecli only reports success once the VM really runs on the target.

---

## ct (LXC containers)

```
pvecli ct start [CTIDS]            Start containers
pvecli ct stop [CTIDS]             Hard stop containers (--yes, --timeout)
pvecli ct shutdown [CTIDS]         Graceful shutdown (--timeout, --force)
pvecli ct reboot [CTIDS]           Reboot containers
pvecli ct add [NODE]               Create a new container (interactive wizard or CLI options)
pvecli ct clone [CTID]             Clone a container
pvecli ct deploy TEMPLATE          Create a ready-to-use container from a file template or a CT template
pvecli ct migrate [CTID] [TARGET]  Migrate a container (--restart when it runs)
pvecli ct edit [CTID]              Edit container configuration
pvecli ct remove [CTIDS]           Delete containers (--purge, --force)
pvecli ct template [CTIDS] [-y]    Convert containers to templates (asks first: irreversible)
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

Containers cannot move live: `ct migrate` on a running container needs `--restart` (or a yes at the prompt), which stops it, moves it and starts it again, with `--timeout` for the shutdown.

### ct deploy

```bash
pvecli ct deploy local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst --hostname web01 --node pve1 \
    --storage local-lvm --disk-size 8G --ip 10.0.0.21/24 --gw 10.0.0.1 --ssh-key ~/.ssh/id_ed25519.pub
pvecli ct deploy 920 --hostname web02 --ip dhcp --cores 2 --memory 1G --swap 0 --disk-size 8G --wait-ip 60
```

The pendant of `vm deploy` for containers. From a file template it creates the container (`POST /lxc`): root disk (`--storage`, whole GiB with `--disk-size`), network (`--ip`, `--gw`, `--ip6`, `--gw6`, `--bridge`, default `vmbr0`, `--vlan`), DNS, SSH keys for root, a root password from an environment variable (`--password-env`), `--features nesting=1`, `--unprivileged/--privileged` (unprivileged unless told otherwise, as PVE does). From the ID of a CT template it clones it (linked when its storage allows it, full with `--full` or `--storage`), then sets the same values with the config; a clone keeps the root password, SSH keys and privilege level of its template, so those options are refused there. Everything is checked before the first write (node, storages, template, pool, free ID, address families), and the command to do it again is printed, without the password. Options left out take Proxmox's own defaults (4G root disk on `local`, 512M of memory and swap).

<!-- new top-level section, after "## snapshot" -->

### ct image (LXC image files)

```
pvecli ct image list [NODE] [STORAGE]   List all LXC images in a storage
pvecli ct image add [NODE] [STORAGE]    Download an LXC image from the Proxmox repository
pvecli ct image remove [NODE] [STORAGE] Remove an LXC image from storage
```

---

## storage

```
pvecli storage add [STORAGE_ID]             Add a shared storage, checking every node reaches it
pvecli storage remove [STORAGE_ID]          Remove a storage definition (data on the backend is kept)
pvecli storage list                         List all storage (--node)
pvecli storage info [NODE] [STORAGE]        Show storage details & config
pvecli storage config [NODE] [STORAGE]      Edit content types interactively
pvecli storage content list [NODE] [STOR]     List content (--type filter)
pvecli storage content add [NODE] [STOR]      Upload from local file (--source-file, --type)
pvecli storage content download [NODE] [STOR] Download from URL (--url, --filename, --type)
pvecli storage content remove [NODE] [STOR]   Delete content
```

### storage add

Creates a **cluster wide** storage: one API call covers every node. What makes it
different from the web UI is the discovery step, which probes the backend from
**every** node before creating anything, and the verification step, which checks
the storage really mounted on each one.

Supported types: `nfs`, `cifs`, `rbd` (external Ceph), `cephfs` (external Ceph),
`pbs`, `iscsi`. Deploying a hyperconverged Ceph is out of scope: use `pveceph`.

```bash
# Discovery: lists the exports the server really offers, from each node
pvecli storage add nfs-iso --type nfs --server 10.0.0.9 --export /srv/iso --content iso

# External Ceph: the keyring is read from a file, never from the command line
pvecli storage add ceph-vm --type rbd --monhost 10.0.0.1,10.0.0.2 \
    --pool rbd --keyring-file /root/ceph.client.admin.keyring

# Restrict to the nodes that can reach the backend
pvecli storage add nfs-iso --type nfs --server 10.0.0.9 --export /srv/iso --nodes pve1,pve2
```

Content types are validated against what the backend supports before any network
call: asking for `images` on a `pbs` storage is refused immediately.

When a node cannot reach the backend, the interactive mode offers to restrict the
storage to the nodes that can. With `--yes` the scope is left untouched and a
warning is printed: the flag means "do not ask", not "decide for me".

Credentials never travel through the command line. Use `--password-env VAR` for
CIFS and PBS, or answer the masked prompt.

---

## pool

```
pvecli pool add [POOLIDS]                   Create pool(s) (--comment)
pvecli pool remove [POOLID]                 Delete a pool (--force)
pvecli pool content add [POOLID] [VMIDS]    Add VMs / CTs to a pool
pvecli pool content remove [POOLID] [VMIDS] Remove VMs / CTs from a pool
pvecli pool export                          Export all pools to JSON (--output)
pvecli pool import [FILE]                   Import pools from JSON (default: pools.json)
pvecli pool list                            List all resource pools
pvecli pool usage [POOLID]                  Show aggregated CPU / memory / disk usage for a pool
pvecli pool info [POOLID]                   Show pool details
```

---

## cluster

```
pvecli cluster preflight           Check what silently breaks a join or a maintenance window
pvecli cluster health              Quorum, node votes, failure tolerance, HA and Ceph in one view
pvecli cluster create [NAME]       Turn a standalone node into the first cluster member
pvecli cluster join [TARGET]       Make a standalone node join the cluster of the profile
pvecli cluster status              Show cluster status
pvecli cluster resources           Show resources (--type vm/ct/node/storage)
pvecli cluster usage               Show node load, guest workload, overhead, pools, storage
pvecli cluster tasks               Show task log (--running, --limit)
pvecli cluster shutdown            Shutdown the entire cluster (orchestrated)
pvecli cluster reboot              Reboot the entire cluster (orchestrated)
pvecli cluster ha disarm           Disarm HA cluster wide before maintenance (PVE 9.2+)
pvecli cluster ha arm              Re-arm HA afterwards
pvecli cluster guests start        Start many guests in one go (filters below)
pvecli cluster guests shutdown     Shut down many guests (--timeout, --force-stop)
pvecli cluster guests suspend      Suspend many VMs (--to-disk to hibernate)
pvecli cluster guests migrate T    Migrate many guests to node T (--with-local-disks)
```

### cluster guests

Pick the guests with `--vms 100,101` or `--vms 100-110`, `--tag`, `--pool` or `--node` (filters combine, and at least one is required). `--max-workers` caps the tasks running at once.

```bash
pvecli cluster guests shutdown --tag lab --timeout 120 --force-stop
pvecli cluster guests start --pool PROD --max-workers 4
pvecli cluster guests migrate pve2 --node pve1     # same as: pvecli node evacuate pve1 pve2
```

Where PVE offers cluster bulk actions, the whole set runs as a single cluster task; older versions get one task per node. A bulk task ends OK even when it skipped a guest (locked, already in the target state...), so pvecli then checks every guest and lists those that did not get there.

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

### cluster preflight

Runs the checks that the web UI does not offer, and that explain most failed joins
and surprises during maintenance:

| Check | What it catches |
|:------|:----------------|
| Nodes online | A node down before you start a maintenance window |
| PVE versions | A join between different releases, which Proxmox refuses. A patch level gap only warns |
| Clock drift | Corosync and the join both depend on synchronized clocks |
| Name resolution | Nodes that resolve each other through neither `/etc/hosts` nor DNS |
| Core services | `pve-cluster`, `pvedaemon`, `pveproxy`, `pvestatd` not running |
| Quorum | Cluster already read only |
| Corosync links | A single link, so no redundancy |
| Quorum resilience | Two nodes without a QDevice |
| Guests present | On a standalone node, joining wipes its `/etc/pve` |

Exit code is `1` when a blocking issue is found, in text mode as in `--json`, so it
can gate a script.

```bash
pvecli cluster preflight
pvecli cluster preflight --max-drift 5 --json
```

### cluster health

Consolidates quorum, per node votes, how many nodes can still be lost, HA and Ceph.

```bash
pvecli cluster health
```

The failure tolerance is deliberately reported as **not computable** when a QDevice
is present or when a node vote is unknown: the arbiter contributes votes that no
endpoint exposes, and a wrong number would be used to decide whether a node can be
shut down. Exit code is `1` when the cluster is not quorate.

### cluster create / join

Both are irreversible through the API: a cluster cannot be renamed nor dissolved,
and leaving a cluster requires reinstalling the node. Both therefore ask you to type
the cluster name to confirm, unless `--yes` is passed.

```bash
# On the profile of a standalone node
pvecli cluster create prod-cluster

# From the profile of the cluster, make another node join it
pvecli cluster join 10.0.0.12
pvecli cluster join 10.0.0.12 --target-password-env NODEPW --peer-password-env PEERPW
```

`join` checks before touching anything, and every check is **measured** rather than
advised: it refuses a node that already belongs to a cluster, refuses a join between
different PVE releases such as 9.1 and 9.2 (Proxmox rejects those) while only warning
on a patch level gap such as 9.1.1 and 9.1.2, refuses when the clocks differ by more
than 30 seconds and warns beyond 2 seconds, warns when guests exist on the node
(joining replaces its `/etc/pve`), and refuses to join a multi link cluster without the
matching link options, which would silently leave corosync without redundancy. Only
`--link0` and `--link1` exist: a cluster on ring2 or above has to be joined with
`pvecm add`, and the error says so rather than naming an option that does not exist.

There is no generic "remember to configure NTP" reminder: Proxmox enables
`systemd-timesyncd` by default, so such a message would be wrong most of the time.
The drift is compared between the cluster and the joining node, and the fix is only
suggested when a real drift is measured.

Note that the Proxmox API exposes no NTP configuration at all, only the timezone, so
neither pvecli nor the web UI can set NTP servers. `cluster preflight` reports the
drift across the whole cluster with the same logic.

Both reconnect on a fresh connection to verify: the node reissues its certificate
and restarts `pveproxy` during the operation, so its own answer cannot be trusted.

Passwords are never taken from the command line. The joining node's password is
prompted or read from `--target-password-env`, and the peer's `root@pam` password
from the profile only when the profile itself authenticates as `root@pam`.

### cluster shutdown / reboot

Orchestrates a safe cluster-wide power operation:

1. Disarms HA in one call on PVE 9.2+ (resources frozen in place), or disables each HA resource on older versions
2. Sets Ceph maintenance flags if Ceph is detected
3. Stops all guests on each node
4. Shuts down / reboots nodes (connected node always last)

Options: `--skip-ceph`, `--skip-ha`, `--timeout/-t` (default 300), `--yes/-y`

Requires double confirmation (type `SHUTDOWN` or `REBOOT`) unless `--yes` is passed.

---

## sdn

Read commands work everywhere. **Every write command is refused** unless the profile
carries `sdn_write: true`, because applying an SDN configuration reloads the network
on every node of the cluster at once. Leave the flag off on production profiles.

```
pvecli sdn list                    Vnets with their zone and their status on every node
pvecli sdn zones                   Zones, their type and where they are deployed
pvecli sdn info [VNET]             Detail a vnet: subnets and the guests attached to it
pvecli sdn health                  Unapplied changes and vnets missing on some nodes
pvecli sdn zone add [ZONE]         Create a zone (--type simple|vlan|qinq|vxlan|evpn)
pvecli sdn zone set [ZONE]         Change a zone (--dhcp/--no-dhcp, --mtu, --nodes, --peers)
pvecli sdn zone remove [ZONE]      Remove a zone (refused while vnets use it)
pvecli sdn vnet add [VNET]         Create a vnet in a zone (--zone, --tag, --alias, --vlanaware)
pvecli sdn vnet remove [VNET]      Remove a vnet (refused while guests or subnets use it)
pvecli sdn subnet add [CIDR]       Attach a subnet (--vnet, --gateway, --snat, --dhcp-range)
pvecli sdn subnet remove [CIDR]    Detach a subnet (--vnet)
pvecli sdn apply                   Apply the pending configuration on every node
pvecli sdn discard                 Drop every pending change
pvecli sdn unlock                  Release a stale SDN lock left by another session
```

On PVE 9.2+, `sdn apply` first shows what the reload changes in the network and FRR configuration of a node (`--no-diff` to skip it). Where PVE has a global SDN lock, apply holds it until the configuration is committed, so a concurrent edit from the web UI or another tool cannot slip in; a lock held by someone else is reported with the way out.

### Enabling writes

Add the flag to the profile in `~/.config/pvecli/config.yaml`:

```yaml
profiles:
  lab:
    host: 10.0.0.10
    sdn_write: true      # absent or false means every SDN write is refused
```

### Workflow

Nothing takes effect until `sdn apply`. Every write command says so and `sdn health`
lists what is waiting, including a subnet added on its own.

```bash
pvecli sdn zone add lab1 --type simple --dhcp
pvecli sdn vnet add labnet --zone lab1 --alias "Lab network"
pvecli sdn subnet add 10.0.30.0/24 --vnet labnet --gateway 10.0.30.1 --snat \
    --dhcp-range 10.0.30.100:10.0.30.200 --dhcp-dns 10.0.0.1
pvecli sdn health          # shows the three pending changes
pvecli sdn apply           # asks you to type the cluster name
```

`sdn apply` waits for the reload task to finish and exits `1` if it fails, then polls
until every vnet reports available on every node. Use `--force` to regenerate the node
configuration when nothing is pending, for instance after installing dnsmasq.

Identifiers for zones and vnets are limited by Proxmox to 2 to 8 characters,
letters and digits only, starting with a letter.

### Zone types

| Type | Needs | Notes |
|:-----|:------|:------|
| `simple` | nothing | Isolated bridge, routed or NAT through the subnet gateway |
| `vlan` | `--bridge` | VLAN tag on an existing bridge |
| `qinq` | `--bridge`, `--tag` | Stacked VLANs |
| `vxlan` | `--peers` | Layer 2 overlay between nodes |
| `evpn` | `--controller`, `--vrf-vxlan` | Routed overlay, needs a controller defined first |

### DHCP

DHCP is a **technology preview** in Proxmox. It needs, on every node:

```bash
apt install dnsmasq
systemctl disable --now dnsmasq   # otherwise it holds port 53 and the per-zone instance fails
```

and the zone itself must carry `--dhcp` (`pvecli sdn zone add ... --dhcp`, or
`pvecli sdn zone set <zone> --dhcp` afterwards). Leases are handed out through the
IPAM, so only guests known to Proxmox get an address.

A DHCP zone without an IPAM never leases anything: dnsmasq ignores every MAC
the IPAM does not know. `sdn zone add` therefore uses the `pve` IPAM unless
`--ipam` says otherwise, and `sdn health` flags any DHCP zone left without one.
Proxmox only accepts adding an IPAM to a zone that has no subnet yet: remove the
subnets, run `pvecli sdn zone set <zone> --dhcp`, add them back, then apply.

---

## ceph

Read only. Deploying a hyperconverged Ceph is deliberately out of scope: installing
the packages has no REST endpoint, and destroying a pool or an OSD is not something a
multi-target CLI should make easy. Use `pveceph` for those.

```
pvecli ceph status                 Health, monitors, OSDs, capacity and active flags
pvecli ceph osd                    OSD tree with usage and device class
pvecli ceph pool                   Pools with size, min_size, PGs and usage
pvecli ceph flags                  All OSD flags and which ones are set
pvecli ceph versions               Version of every daemon, to spot the ones left behind
pvecli ceph log [NODE]             Tail of the Ceph log (--limit)
```

On a cluster without Ceph, every command says so and exits `0`.

`ceph pool` warns when a pool has `min_size` below 2, where a single OSD failure can
lose writes. `ceph status` warns when a flag is set, since a forgotten `noout` hides a
failed OSD for as long as it stays on. `cluster shutdown` and `cluster reboot` set
those flags for you, and print the `ceph osd unset` commands to run once the nodes are
back up: clearing them is deliberate rather than automatic, since nothing on the way
back tells pvecli the cluster is healthy again.

Looping over profiles gives a fleet wide view that the web UI cannot produce, since
one web UI shows one cluster:

```bash
for p in cluster-a cluster-b cluster-c; do
    echo "== $p"; pvecli ceph status -p "$p" --json | jq -r .health
done
```

---

## find

Where something lives and what it belongs to, read only, on one or many clusters.

```bash
pvecli find 10.20.3.14 -p '*'           # an IP: guests and infrastructure
pvecli find 10.20.3.0/24                # a network
pvecli find bc:24:11:ee:5a:c4           # a MAC
pvecli find vm-105-disk-1               # which guest uses this volume
pvecli find web01.example.com           # resolved, then searched as its IPs
pvecli find web                         # name, notes
```

An IP is searched in the guest configs (cloud-init, LXC), live from the guest agent and LXC interfaces, in the SDN IPAM, and in the infrastructure: node addresses, storage servers (NFS, CIFS, PBS, Ceph monitors), SDN gateways, firewall aliases and IP sets. Every answer says where it came from.

Filters narrow the query, or list guests on their own:

```
--on-vnet / --on-bridge / --vlan       attached to a network
--on-storage / --iso                   with a disk on a storage, an ISO mounted
--rule / --pool / --tag / --snapshot   HA rule, pool, tag, snapshot name
--stopped --running --template --locked
--no-agent --no-backup --no-tags --old-snapshots 30d
--min-ram 8G --min-cores 8 --os windows
--tasks                                last actions run on each guest (who, what, when)
```

Access: `pvecli find --access 105` lists who can act on guest 105 (ACL entries on the guest, its pool and above); `pvecli find --user alice@pve` what a user or token can reach.

---

## audit

Read-only health and hygiene report, per cluster or across many:

```bash
pvecli audit
pvecli audit -p '*' --problems          # only WARN and FAIL
pvecli audit -p @prod --json            # for a cron job or a dashboard
```

Checks: certificates expiring (`--cert-days`, default 30), expired users and tokens, tokens without privilege separation, broad Administrator grants, datacenter firewall disabled, PVE versions mixed across nodes, subscription, pending updates, guests in no backup job, backup freshness (last backup older than `--backup-age`, default 2d, or failed), snapshots older than `--snapshot-age` (default 30d), ISOs left mounted, orphan disks (volumes of guests that no longer exist, `--skip-orphans` to skip the storage scan), HA disarmed, Ceph health and maintenance flags left set.

Exit code: 0 when nothing fails (warnings included), 2 when a check fails, 1 when the audit could not run.

---

## snapshot

Snapshots across many guests, VMs and containers alike. Guests are picked with `--vms`, `--tag`, `--pool`, `--node`, or `--all`.

```bash
pvecli snapshot add pre-upgrade --tag web          # same snapshot on every web guest
pvecli snapshot list --older-than 30d -p '*'
pvecli snapshot prune --older-than 7d --name-prefix pre- --keep 1
```

`add` skips guests that already have a snapshot with that name. `prune` always shows what it would delete and asks first; `--keep N` spares the N most recent matching snapshots of each guest.

---

## deploy

Many VMs and containers from one YAML file, on one or several clusters.

```bash
pvecli deploy lab.yaml --dry-run                  # check everything, show the plan
pvecli deploy lab.yaml -y --parallel 8 --wait-ip 120
```

The file has `defaults` (common keys, plus `vm:` and `ct:` sections for one type) and a list of `guests`, each `type: vm` (same steps as `vm deploy`) or `type: ct` (same steps as `ct deploy`). Keys follow the options: `name`, `template`, `id`, `node`, `storage`, `full`, `cores`, `memory`, `disk_size`, `tags`, `pool`, `ip`, `gw`, `ip6`, `gw6`, `dns`, `search_domain`, `ssh_key`, `password_env`, `power_on`; `user` and `upgrade` for a VM; `swap`, `bridge`, `vlan`, `unprivileged`, `features` for a container. `null` clears a default. A guest without `profile:` uses `-p`, then the default profile; the plan is grouped by cluster. See `examples/deploy.yaml`.

- **Series**: `count: 3` with `name: web{n}` makes web1 to web3 (`start: 10` for the first number, `{n:02}` for web01), with the ID and a static address stepping by one; a series leaving its subnet is refused.
- **Checks first**: unknown keys, a `password` key (only `password_env` is accepted), names, IDs or addresses given twice, templates, nodes, storages and pools are checked on every cluster before the first write, and every problem is listed at once.
- **Idempotent**: a guest whose name exists on its cluster is shown as present and skipped, never overwritten; a name held by the other type, or an `id` taken by another guest, is a conflict and nothing is created. A second run creates nothing.
- **Execution**: `--parallel N` guests at once (4 by default); free IDs come from `/cluster/nextid` and are reserved for the whole run; clones of one CT template take turns (PVE locks the template during a clone). A failure does not stop the others; the summary gives each guest's ID, cluster, address (`--wait-ip`) and error, lists what a failure left on the cluster with the command to remove it, and exits 1. Nothing is deleted automatically.

---

## backup

Backups of every guest, through the Proxmox API only. They are found on every enabled storage holding `backup` content: a shared storage (PBS, NFS...) is read once, a local one on each node. `list`, `status` and `jobs list` take several profiles (`-p '*'`) and `--json`.

```bash
pvecli backup list --vmid 100 --storage pbs        # age, size, protection, notes
pvecli backup status --older-than 2d --failed -p '*'
pvecli backup run 100,101 --storage pbs            # or --tag web, --pool prod, --node pve1, --all
pvecli backup run --tag web --mode stop --notes 'before upgrade {{guestname}}' --protected -y
pvecli backup restore pbs:backup/vm/100/2026-11-18T02:00:00Z --vmid 900 --unique --start
pvecli backup prune local --keep-last 3 --keep-daily 7 --dry-run
pvecli backup files pbs:backup/ct/200/2026-11-18T02:00:00Z /root.pxar.didx/etc
```

```
pvecli backup list                         Backups per guest (--vmid, --tag, --pool, --storage)
pvecli backup status                       Covering job, last backup, last vzdump run (--older-than, --failed)
pvecli backup run [IDS]                    Back up now, one vzdump per node (--storage, --mode, --compress, --notes, --protected)
pvecli backup restore VOLID                Restore as a new guest (--vmid, --node, --storage, --start, --unique, --force)
pvecli backup remove VOLID...              Delete backups (asks first; a protected backup is refused)
pvecli backup protect VOLID...             Protect from pruning and removal (unprotect to undo)
pvecli backup notes VOLID TEXT             Set the notes of a backup
pvecli backup prune STORAGE                Preview, then prune by retention (--keep-last, --keep-daily, --keep-weekly, --keep-monthly, --keep-yearly, --vmid, --type, --dry-run)
pvecli backup files VOLID [PATH]           Browse a PBS backup, read only
pvecli backup jobs list                    Scheduled jobs
pvecli backup jobs add                     New job (--schedule, --storage, --vmid | --all [--exclude] | --pool, --keep-*)
pvecli backup jobs edit ID                 Change only the options given
pvecli backup jobs enable ID / disable ID / remove ID
pvecli backup jobs run ID                  Run a job now with its own options
```

`run` sends `compress=zstd` and `remove=0` unless told otherwise: PVE alone would write an uncompressed archive and prune older backups by the storage retention. A guest counts as backed up only when the task log says so and a new archive of it is on the storage: a task can end OK after dropping a guest that moved to another node. Exit code 1 when a guest was not backed up.

`restore` takes the next free ID by default and refuses an existing one. With `--force` the guest must be stopped and its VMID typed again; the restore then runs on its node. A container restored without `--storage` goes back to the storage of its root disk (PVE alone would use `local`).

`status` says "not checked" rather than OK when something cannot be read: a storage that does not answer, backup jobs without `Sys.Audit` on `/`, or the vzdump tasks of a node without `Sys.Audit` on that node (PVE then hides other users' tasks).

`jobs run`: PVE has no "run job" endpoint, the job's options go to vzdump on each node with `job-id` (root@pam only; without it, the run is a plain backup).

`audit` gains a "Backup freshness" check: guests of an enabled job whose last backup is older than `--backup-age` (default 2d) warn, a failed last run fails.

---

## tag (global)

```
pvecli tag list                    List all tags across the cluster
pvecli tag add [TAG]               Add / update a tag color (--color)
pvecli tag edit [TAG]              Rename a tag and/or change its color (--name, --color)
pvecli tag remove [TAGS]           Remove tag(s) from all VMs and CTs
pvecli tag export                  Export tags and colors to JSON (--output)
pvecli tag import [FILE]           Import tags and colors from JSON (default: tags.json)
pvecli tag color init              Initialize the color palette
pvecli tag color list              List palette colors
pvecli tag color add [NAME] [HEX]  Add a color to the palette
pvecli tag color remove [NAME]     Remove a color from the palette
```
