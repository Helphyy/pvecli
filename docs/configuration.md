# Configuration Reference

## File location

```
~/.config/pvecli/config.yaml
```

Permissions are automatically set to `600` (owner read/write only).

---

## Structure

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

---

## Fields

### Top-level

| Field | Type | Description |
|:------|:-----|:------------|
| `default_profile` | string | Profile used when `--profile` is not specified |

### Profile

| Field | Type | Default | Description |
|:------|:-----|:--------|:------------|
| `host` | string | - | Proxmox hostname, IPv4 or IPv6 (bare or bracketed, e.g. `2001:db8::1` or `[2001:db8::1]`) |
| `port` | integer | `8006` | Proxmox API port |
| `verify_ssl` | boolean | `true` | Verify TLS certificate. Set to `false` for self-signed certs |
| `auth` | object | - | Authentication block |

### auth block

| Field | Type | Description |
|:------|:-----|:------------|
| `type` | string | Always `"token"` (API token auth) |
| `user` | string | Proxmox user, e.g. `root@pam` or `admin@pve` |
| `token_name` | string | Token ID as created in the Proxmox UI |
| `token_value` | string | Token secret (UUID format) |

---

## Managing profiles

```bash
pvecli config add [NAME]       # Interactive wizard - creates a new profile
pvecli config edit [NAME]      # Edit an existing profile
pvecli config remove [NAME]    # Remove one profile (or --all)
pvecli config default [NAME]   # Change the default profile
pvecli config list             # Show all profile names
pvecli config info [NAME]      # Show profile details (token secret is masked, --all for all)
pvecli config test             # Verify connection to the active profile
pvecli config login            # Open the Proxmox web UI in your browser
```

---

## Creating an API token in Proxmox

1. Open the Proxmox web UI
2. Navigate to **Datacenter > Permissions > API Tokens**
3. Click **Add**, select the user, give the token a name
4. **Privilege Separation** :
   - *Checked* (recommended) : the token only has the permissions explicitly granted to it. You must then grant the privileges below to the token (Datacenter > Permissions > Add > API Token Permission).
   - *Unchecked* : the token inherits all of the user's permissions. Simpler but less safe ; use only with a dedicated technical user.
5. Copy the **Token ID** and **Secret**. The secret is only displayed once.

### Quick setup (recommended)

Create a dedicated user, give it the built-in `PVEAdmin` role on `/`, then create the token. `PVEAdmin` covers almost everything pvecli does (VM/CT/storage/pool/audit), without giving access to user/permission management or arbitrary file editing.

```
pveum user add pvecli@pve
pveum aclmod / -user pvecli@pve -role PVEAdmin
pveum user token add pvecli@pve cli --privsep 0
```

The output of the last command contains the token value : keep it preciously.

For node and cluster shutdown/reboot, add `Sys.PowerMgmt` (not included in `PVEAdmin`) :

```
pveum aclmod /nodes -user pvecli@pve -role PVEAuditor -propagate 1
pveum role add PVECLIPower -privs "Sys.PowerMgmt,Sys.Console"
pveum aclmod / -user pvecli@pve -role PVECLIPower
```

### Detailed minimum permissions

If you prefer to grant only what's needed, here is the mapping feature by feature :

| Feature | Privilege | Path |
|:--------|:----------|:-----|
| List, inspect VMs/CTs | `VM.Audit` | `/vms` |
| Start, stop, shutdown, reboot, suspend | `VM.PowerMgmt` | `/vms` |
| VNC, SSH, RDP launchers | `VM.Console` | `/vms` |
| Create / edit VMs and containers | `VM.Allocate`, `VM.Config.Disk`, `VM.Config.CPU`, `VM.Config.Memory`, `VM.Config.Network`, `VM.Config.Options`, `VM.Config.HWType`, `VM.Config.CDROM` | `/vms` |
| Delete VMs / containers | `VM.Allocate` | `/vms` |
| Clone, convert to template | `VM.Clone` | `/vms` |
| Snapshots (list, create, rollback, remove) | `VM.Snapshot` | `/vms` |
| Tags (add / remove on a VM) | `VM.Config.Options` | `/vms` |
| Run commands via QEMU Guest Agent (`vm exec`) | `VM.Monitor` | `/vms` |
| HA management (`vm ha`, `ct ha`) | `Sys.Console` | `/` |
| List storage and content | `Datastore.Audit` | `/storage` |
| Upload / download / delete storage content | `Datastore.AllocateSpace`, `Datastore.AllocateTemplate` | `/storage` |
| Allocate disks during VM/CT creation | `Datastore.AllocateSpace` | `/storage/<id>` |
| Node info, cluster status, tasks | `Sys.Audit` | `/nodes` |
| Node and cluster shutdown / reboot | `Sys.PowerMgmt`, `Sys.Console` | `/nodes` (or `/`) |
| Open node VNC shell, SSH | `Sys.Console` | `/nodes` |
| Pool listing | `Pool.Audit` | `/pool` |
| Pool create / delete / membership | `Pool.Allocate` | `/pool` |
| Tag color palette (cluster options) | `Sys.Modify` | `/` |

> The simplest secure setup is still `PVEAdmin` + (`Sys.PowerMgmt`, `Sys.Console`) on a dedicated user. Refine only if your security policy requires it.
