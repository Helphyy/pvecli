"""Record the backup endpoints of a lab as test fixtures (GET requests only).

    .venv/bin/python -m tests.tools.record_backup_fixtures <profile> <version-label>
    e.g. python -m tests.tools.record_backup_fixtures lab1 pve-9.1

Needs at least one backup on a non shared storage and, for the PBS files,
a reachable Proxmox Backup Server storage. Writes
tests/fixtures/<version-label>/backup_*.json, secrets removed the same way
as record_fixtures.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any
from urllib.parse import quote

from src.api.client import ProxmoxClient
from src.api.exceptions import PVECliError
from src.config import ConfigManager
from tests.tools.record_fixtures import OUT, scrub


async def record(profile: str, label: str) -> None:
    out = OUT / label
    out.mkdir(parents=True, exist_ok=True)
    async with ProxmoxClient(ConfigManager().get_profile(profile)) as c:
        nodes = [n["node"] for n in await c.get("/nodes") if n.get("status") == "online"]
        storages = await c.get("/storage")
        local = [s["storage"] for s in storages if "backup" in s.get("content", "") and not s.get("shared")]
        pbs = [s["storage"] for s in storages if s.get("type") == "pbs" and not s.get("disable")]
        # The node and local storage holding a backup
        node, store, item = nodes[0], local[0] if local else "local", None
        for n in nodes:
            for s in local:
                items = await c.get(f"/nodes/{n}/storage/{s}/content", params={"content": "backup"})
                if items:
                    node, store, item = n, s, items[0]
                    break
            if item:
                break
        tasks = await c.get(f"/nodes/{node}/tasks", params={"typefilter": "vzdump", "limit": 20})
        calls: dict[str, tuple[str, dict[str, Any] | None]] = {
            "backup_content_local": (f"/nodes/{node}/storage/{store}/content", {"content": "backup"}),
            "backup_vzdump_tasks": (f"/nodes/{node}/tasks", {"typefilter": "vzdump", "limit": 20}),
            "backup_jobs": ("/cluster/backup", None),
            "backup_vzdump_defaults": (f"/nodes/{node}/vzdump/defaults", None),
            "backup_prune_unconfigured": (f"/nodes/{node}/storage/{store}/prunebackups", None),
        }
        if item:
            v = quote(item["volid"], safe="")
            calls |= {
                "backup_volume_attributes": (f"/nodes/{node}/storage/{store}/content/{v}", None),
                "backup_prune_preview": (
                    f"/nodes/{node}/storage/{store}/prunebackups",
                    {"prune-backups": "keep-last=1", "vmid": item["vmid"]},
                ),
                "backup_file_restore_not_pbs": (
                    f"/nodes/{node}/storage/{store}/file-restore/list",
                    {"volume": item["volid"], "filepath": "/"},
                ),
                "backup_nextid_taken": ("/cluster/nextid", {"vmid": item["vmid"]}),
                f"backup_extractconfig_{item['subtype']}": (
                    f"/nodes/{node}/vzdump/extractconfig", {"volume": item["volid"]},
                ),
            }
        # One log per task outcome seen (OK, job errors, ...)
        seen: set[str] = set()
        for t in tasks:
            status = str(t.get("status", "")).replace(" ", "_").lower()
            kind = "single" if t.get("id") else "multi"
            name = f"backup_vzdump_log_{kind}_{status}"
            if status and name not in seen:
                seen.add(name)
                calls[name] = (f"/nodes/{node}/tasks/{t['upid']}/log", {"limit": 1000})
        for s in pbs:
            calls["backup_content_pbs"] = (f"/nodes/{nodes[0]}/storage/{s}/content", {"content": "backup"})
            try:
                items = await c.get(f"/nodes/{nodes[0]}/storage/{s}/content", params={"content": "backup"})
            except PVECliError:
                items = []
            if items:
                calls["backup_file_restore_pbs_root"] = (
                    f"/nodes/{nodes[0]}/storage/{s}/file-restore/list",
                    {"volume": items[0]["volid"], "filepath": "/"},
                )
            break

        for name, (path, params) in calls.items():
            try:
                data: Any = await c.get(path, params=params)
            except PVECliError as e:
                data = {"__error__": str(e), "__status__": getattr(e, "status_code", None)}
            (out / f"{name}.json").write_text(json.dumps(scrub(data), indent=1, sort_keys=True) + "\n")
            print(f"{label}/{name}")


if __name__ == "__main__":
    asyncio.run(record(sys.argv[1], sys.argv[2]))
