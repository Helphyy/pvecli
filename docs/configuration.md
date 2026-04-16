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
| `host` | string | - | Proxmox hostname or IP |
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
pvecli config show [NAME]      # Show profile details (token secret is masked)
pvecli config test             # Verify connection to the active profile
pvecli config login            # Open the Proxmox web UI in your browser
```

---

## Creating an API token in Proxmox

1. Open the Proxmox web UI
2. Navigate to **Datacenter → Permissions → API Tokens**
3. Click **Add**, select the user, give the token a name, and uncheck *Privilege Separation* if you want full access
4. Copy the **Token ID** and **Secret** - the secret is only shown once

Recommended minimum permissions for pvecli:

| Privilege | Path | Notes |
|:----------|:-----|:------|
| `VM.Audit` | `/vms` | List and inspect VMs/CTs |
| `VM.PowerMgmt` | `/vms` | Start, stop, reboot |
| `VM.Snapshot` | `/vms` | Create and manage snapshots |
| `VM.Clone` | `/vms` | Clone VMs/CTs |
| `VM.Config.*` | `/vms` | Edit VM/CT configuration |
| `Datastore.Audit` | `/storage` | List storage and content |
| `Datastore.AllocateSpace` | `/storage` | Upload content |
| `Sys.Audit` | `/nodes` | Node information |
| `Sys.PowerMgmt` | `/nodes` | Node shutdown/reboot |
| `Pool.Audit` | `/pool` | Pool listing |
| `Pool.Allocate` | `/pool` | Create/delete pools |
