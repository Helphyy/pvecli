"""Record real Proxmox API responses as test fixtures (read-only calls).

    .venv/bin/python -m tests.tools.record_fixtures <profile> <version-label>
    e.g. python -m tests.tools.record_fixtures lab1 pve-9.1

Writes tests/fixtures/<version-label>/<name>.json. Secrets are removed
(certificate PEM, tickets, tokens, passwords). Only GET requests are sent:
safe on any cluster, but use a lab: fixtures end up in the repository.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

from src.api.client import ProxmoxClient
from src.api.exceptions import PVECliError
from src.config import ConfigManager

OUT = Path(__file__).resolve().parent.parent / "fixtures"
SECRET_KEYS = {"pem", "ticket", "CSRFPreventionToken", "password", "token_value", "keyring", "value"}


def scrub(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: ("<redacted>" if k in SECRET_KEYS and isinstance(v, str) else scrub(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [scrub(x) for x in data]
    return data


async def record(profile: str, label: str) -> None:
    out = OUT / label
    out.mkdir(parents=True, exist_ok=True)
    async with ProxmoxClient(ConfigManager().get_profile(profile)) as c:
        nodes = [n["node"] for n in await c.get("/nodes")]
        node = nodes[0]
        guests = await c.get("/cluster/resources", params={"type": "vm"}) or []
        qemu = next((g for g in guests if g["type"] == "qemu" and not g.get("template")), None)
        lxc = next((g for g in guests if g["type"] == "lxc"), None)

        calls: dict[str, tuple[str, dict[str, Any] | None]] = {
            "version": ("/version", None),
            "nodes": ("/nodes", None),
            "cluster_status": ("/cluster/status", None),
            "cluster_resources_vm": ("/cluster/resources", {"type": "vm"}),
            "cluster_options": ("/cluster/options", None),
            "storage": ("/storage", None),
            "ha_resources": ("/cluster/ha/resources", None),
            "ha_groups": ("/cluster/ha/groups", None),
            "ha_rules": ("/cluster/ha/rules", None),
            "ha_status_current": ("/cluster/ha/status/current", None),
            "ha_manager_status": ("/cluster/ha/status/manager_status", None),
            "sdn_zones": ("/cluster/sdn/zones", None),
            "sdn_vnets": ("/cluster/sdn/vnets", None),
            "sdn_ipams": ("/cluster/sdn/ipams", None),
            "sdn_ipam_pve_status": ("/cluster/sdn/ipams/pve/status", None),
            "sdn_dry_run": ("/cluster/sdn/dry-run", {"node": node}),
            "bulk_action_guest": ("/cluster/bulk-action/guest", None),
            "firewall_options": ("/cluster/firewall/options", None),
            "firewall_aliases": ("/cluster/firewall/aliases", None),
            "access_users_full": ("/access/users", {"full": 1}),
            "access_acl": ("/access/acl", None),
            "backup_not_backed_up": ("/cluster/backup-info/not-backed-up", None),
            "ceph_status": ("/cluster/ceph/status", None),
            "ceph_flags": ("/cluster/ceph/flags", None),
            "node_version": (f"/nodes/{node}/version", None),
            "node_time": (f"/nodes/{node}/time", None),
            "node_dns": (f"/nodes/{node}/dns", None),
            "node_hosts": (f"/nodes/{node}/hosts", None),
            "node_subscription": (f"/nodes/{node}/subscription", None),
            "node_certificates": (f"/nodes/{node}/certificates/info", None),
            "node_apt_update": (f"/nodes/{node}/apt/update", None),
        }
        if qemu:
            base = f"/nodes/{qemu['node']}/qemu/{qemu['vmid']}"
            calls |= {
                "qemu_config": (f"{base}/config", None),
                "qemu_status_current": (f"{base}/status/current", None),
                "qemu_migrate_preconditions": (f"{base}/migrate", None),
                "qemu_snapshot": (f"{base}/snapshot", None),
                "qemu_agent_interfaces": (f"{base}/agent/network-get-interfaces", None),
            }
        if lxc:
            base = f"/nodes/{lxc['node']}/lxc/{lxc['vmid']}"
            calls |= {
                "lxc_config": (f"{base}/config", None),
                "lxc_interfaces": (f"{base}/interfaces", None),
                "lxc_migrate_preconditions": (f"{base}/migrate", None),
            }

        for name, (path, params) in calls.items():
            try:
                data: Any = await c.get(path, params=params)
            except PVECliError as e:
                # Errors are fixtures too: how this version refuses
                data = {"__error__": str(e), "__status__": getattr(e, "status_code", None)}
            text = json.dumps(scrub(data), indent=1, sort_keys=True)
            # Lab passwords never belong in a repository
            text = re.sub(r"LabSdn2026pve", "<redacted>", text)
            (out / f"{name}.json").write_text(text + "\n")
            print(f"{label}/{name}")


if __name__ == "__main__":
    asyncio.run(record(sys.argv[1], sys.argv[2]))
