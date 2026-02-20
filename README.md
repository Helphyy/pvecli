# pvecli

A modern, interactive command-line interface for managing Proxmox VE clusters.

Built with [Typer](https://typer.tiangolo.com/), [Rich](https://rich.readthedocs.io/), and [simple-term-menu](https://github.com/IngoMeyer441/simple-term-menu) for a smooth terminal experience.

## Highlights

- **Interactive menus** — select VMs, containers, nodes, tags, and storage from arrow-key menus instead of memorizing IDs
- **Multi-target operations** — `pvecli vm stop 100,101,102` or pick several items from a menu
- **Multi-cluster profiles** — switch between homelab and production with `--profile`
- **Remote access** — built-in VNC, SSH (with jump host), and RDP launchers
- **Rich output** — tables, spinners, colors, and confirmation prompts
- **Async under the hood** — fast parallel API calls via httpx

## Installation

Requires **Python 3.10+**.

```bash
pipx install git+https://github.com/Helphyy/pvecli.git
```

## Quick Start

### 1. Create an API Token in Proxmox

1. Open the Proxmox web UI
2. Go to **Datacenter > Permissions > API Tokens**
3. Click **Add**, create a token, and copy the token ID + secret

### 2. Configure pvecli

```bash
pvecli config add
```

The interactive wizard asks for host, port, user, token name, and token value.

### 3. Test the connection

```bash
pvecli config test
```

### 4. Start using it

```bash
pvecli node list
pvecli vm list
pvecli ct list
```

## Configuration

Stored in `~/.config/pvecli/config.yaml` (file permissions `600`).

```yaml
default_profile: homelab

profiles:
  homelab:
    host: "192.168.1.100"
    port: 8006
    verify_ssl: false
    auth:
      type: "token"
      user: "root@pam"
      token_name: "pvecli"
      token_value: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

  production:
    host: "pve.example.com"
    port: 8006
    verify_ssl: true
    auth:
      type: "token"
      user: "automation@pve"
      token_name: "cli"
      token_value: "yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy"
```

## Commands

Every command supports `--profile, -p` to target a specific cluster profile.
Most commands work **interactively** when called without arguments — just navigate the menu.

### Config

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

### Node

```
pvecli node list                   List all cluster nodes
pvecli node show [NODE]            Show node details (--all for all)
pvecli node vnc [NODE]             Open VNC shell to a node
pvecli node ssh [NODE]             SSH into a node
```

### VM

```
pvecli vm list                     List all VMs (--node, --status filters)
pvecli vm show [VMID]              Show detailed VM info
pvecli vm start [VMIDS]            Start VMs
pvecli vm stop [VMIDS]             Hard stop VMs (--yes, --timeout)
pvecli vm shutdown [VMIDS]         Graceful shutdown (--timeout, --force)
pvecli vm reboot [VMIDS]           Reboot VMs
pvecli vm suspend [VMIDS]          Suspend VMs
pvecli vm resume [VMIDS]           Resume suspended VMs
pvecli vm clone [VMID]             Clone a VM (many options, interactive)
pvecli vm edit [VMID]              Edit VM configuration interactively
pvecli vm remove [VMIDS]           Delete VMs (--purge, --force)
pvecli vm exec [VMID] [CMD]        Run command via QEMU Guest Agent
pvecli vm vnc [VMID]               Open VNC console
pvecli vm ssh [VMID]               SSH into VM (--jump for jump host)
pvecli vm rdp [VMID]               Open RDP session
```

#### VM Tags

```
pvecli vm tag list [VMID]          List tags
pvecli vm tag add [VMID] [TAGS]    Add tags (--replace to overwrite)
pvecli vm tag remove [VMID] [TAGS] Remove tags
```

#### VM Snapshots

```
pvecli vm snapshot list [VMID]              List snapshots
pvecli vm snapshot add [VMID] [NAME]        Create snapshot (--description)
pvecli vm snapshot rollback [VMID] [NAME]   Rollback (--reboot)
pvecli vm snapshot remove [VMID] [NAME]     Delete snapshot
```

### Container (LXC)

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
pvecli ct vnc [CTID]               Open VNC console
pvecli ct ssh [CTID]               SSH into container (--jump)
```

#### CT Tags

```
pvecli ct tag list [CTID]          List tags
pvecli ct tag add [CTID] [TAGS]    Add tags (--replace to overwrite)
pvecli ct tag remove [CTID] [TAGS] Remove tags
```

#### CT Snapshots

```
pvecli ct snapshot list [CTID]              List snapshots
pvecli ct snapshot add [CTID] [NAME]        Create snapshot
pvecli ct snapshot rollback [CTID] [NAME]   Rollback (--reboot)
pvecli ct snapshot remove [CTID] [NAME]     Delete snapshot
```

### Storage

```
pvecli storage list                         List all storage (--node)
pvecli storage show [NODE] [STORAGE]        Show storage details & config
pvecli storage config [NODE] [STORAGE]      Edit content types interactively
pvecli storage content list [NODE] [STOR]   List content (--type filter)
pvecli storage content add [NODE] [STOR]    Upload ISO/template/import
pvecli storage content remove [NODE] [STOR] Delete content
```

### Pool

```
pvecli pool list                            List all resource pools
pvecli pool show [POOLID]                   Show pool details
pvecli pool add [POOLID]                    Create a pool (--comment)
pvecli pool remove [POOLID]                 Delete pool (--force)
pvecli pool content add [POOLID] [VMIDS]    Add VMs/CTs to pool
pvecli pool content remove [POOLID] [VMIDS] Remove VMs/CTs from pool
```

### Cluster

```
pvecli cluster status              Show cluster status
pvecli cluster resources           Show resources (--type vm/ct/node/storage)
pvecli cluster tasks               Show tasks (--running, --limit)
```

### Tag (global)

```
pvecli tag list                    List all tags across the cluster
pvecli tag add [TAG]               Add/update a tag color (--color)
pvecli tag remove [TAGS]           Remove tag(s) from all VMs/CTs
pvecli tag color init              Initialize color palette
pvecli tag color list              List palette colors
pvecli tag color add [NAME] [HEX]  Add a color to palette
pvecli tag color remove [NAME]     Remove a color from palette
```

## Examples

### Manage multiple clusters

```bash
# Add a production profile
pvecli config add production

# Run a command against production
pvecli vm list --profile production

# Change default profile
pvecli config default production
```

### Day-to-day VM operations

```bash
# Start several VMs at once
pvecli vm start 100,101,102

# Graceful shutdown with 2-minute timeout
pvecli vm shutdown 200 --timeout 120

# Snapshot before maintenance
pvecli vm snapshot add 100 pre-update --description "Before system update"

# Clone a VM for testing
pvecli vm clone 100 --name test-vm

# SSH into a VM through the Proxmox node as jump host
pvecli vm ssh 100 --jump
```

### Container workflow

```bash
# Create a container interactively
pvecli ct add

# Execute commands inside
pvecli ct ssh 200

# Tag for organization
pvecli ct tag add 200 web,production
```

### Storage management

```bash
# Upload an ISO
pvecli storage content add

# List ISO images
pvecli storage content list --type iso
```

## Shell Completion

```bash
# Bash
pvecli --install-completion bash && source ~/.bashrc

# Zsh
pvecli --install-completion zsh && source ~/.zshrc

# Fish
pvecli --install-completion fish
```

## Development

```bash
git clone https://github.com/Helphyy/pvecli.git
cd pvecli
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

```bash
ruff format .          # Format
ruff check .           # Lint
mypy src/pvecli        # Type check
pytest                 # Test
```

## Security

- Config file permissions are set to `600` (owner-only read/write)
- API token authentication is recommended over password auth
- SSL verification is enabled by default — use `verify_ssl: false` only for self-signed certificates

## License

MIT
