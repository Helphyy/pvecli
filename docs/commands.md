# Command Reference

Every command supports `--profile, -p` to target a specific cluster profile.
Most commands work **interactively** when called without arguments.

---

## config

```
pvecli config add [NAME]           Add a new profile (interactive wizard)
pvecli config edit [NAME]          Edit an existing profile
pvecli config remove [NAME]        Remove a profile (--all for all)
pvecli config default [NAME]       Set the default profile
pvecli config list                 List all profiles
pvecli config show [NAME]          Show profile details (--all for all)
pvecli config test                 Test connection to Proxmox
pvecli config login                Open Proxmox web UI in browser
```

---

## node

```
pvecli node list                   List all cluster nodes
pvecli node show [NODE]            Show node details (--all for all)
pvecli node vnc [NODE]             Open VNC shell to a node (background by default, --no-background)
pvecli node ssh [NODE]             SSH into a node
```

---

## vm

```
pvecli vm list                     List all VMs (--node, --status filters)
pvecli vm show [VMID]              Show detailed VM info
pvecli vm start [VMIDS]            Start VMs
pvecli vm stop [VMIDS]             Hard stop VMs (--yes, --timeout)
pvecli vm shutdown [VMIDS]         Graceful shutdown (--timeout, --force)
pvecli vm reboot [VMIDS]           Reboot VMs
pvecli vm suspend [VMIDS]          Suspend VMs
pvecli vm resume [VMIDS]           Resume suspended VMs
pvecli vm lock [VMID] [TYPE]       Lock a VM (requires root@pam)
pvecli vm unlock [VMID]            Unlock a VM (requires root@pam)
pvecli vm add [NODE]               Create a new VM (interactive)
pvecli vm clone [VMID]             Clone a VM (interactive, many options)
pvecli vm edit [VMID]              Edit VM configuration interactively
pvecli vm remove [VMIDS]           Delete VMs (--purge, --force)
pvecli vm template [VMIDS]         Convert VMs to templates (single or comma-separated)
pvecli vm exec [VMID] [CMD]        Run a command via QEMU Guest Agent
pvecli vm vnc [VMID]               Open VNC console (background by default, --no-background)
pvecli vm ssh [VMID]               SSH into VM (--jump for jump host)
pvecli vm rdp [VMID]               Open RDP session
```

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

---

## ct (LXC containers)

```
pvecli ct list                     List all containers (--node, --status)
pvecli ct show [CTID]              Show detailed container info
pvecli ct add [NODE]               Create a new container (interactive)
pvecli ct start [CTIDS]            Start containers
pvecli ct stop [CTIDS]             Hard stop containers (--yes, --timeout)
pvecli ct shutdown [CTIDS]         Graceful shutdown (--timeout, --force)
pvecli ct reboot [CTIDS]           Reboot containers
pvecli ct clone [CTID]             Clone a container
pvecli ct edit [CTID]              Edit container configuration
pvecli ct remove [CTIDS]           Delete containers (--purge, --force)
pvecli ct template [CTIDS]         Convert containers to templates (single or comma-separated)
pvecli ct vnc [CTID]               Open VNC console (background by default, --no-background)
pvecli ct ssh [CTID]               SSH into container (--jump)
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
pvecli storage show [NODE] [STORAGE]        Show storage details & config
pvecli storage config [NODE] [STORAGE]      Edit content types interactively
pvecli storage content list [NODE] [STOR]   List content (--type filter)
pvecli storage content add [NODE] [STOR]    Upload ISO / template / import disk
pvecli storage content remove [NODE] [STOR] Delete content
```

---

## pool

```
pvecli pool list                            List all resource pools
pvecli pool show [POOLID]                   Show pool details
pvecli pool add [POOLIDS]                   Create pool(s) (--comment)
pvecli pool remove [POOLID]                 Delete a pool (--force)
pvecli pool content add [POOLID] [VMIDS]    Add VMs / CTs to a pool
pvecli pool content remove [POOLID] [VMIDS] Remove VMs / CTs from a pool
```

---

## cluster

```
pvecli cluster status              Show cluster status
pvecli cluster resources           Show resources (--type vm/ct/node/storage)
pvecli cluster tasks               Show task log (--running, --limit)
```

---

## tag (global)

```
pvecli tag list                    List all tags across the cluster
pvecli tag add [TAG]               Add / update a tag color (--color)
pvecli tag remove [TAGS]           Remove tag(s) from all VMs and CTs
pvecli tag color init              Initialize the color palette
pvecli tag color list              List palette colors
pvecli tag color add [NAME] [HEX]  Add a color to the palette
pvecli tag color remove [NAME]     Remove a color from the palette
```
