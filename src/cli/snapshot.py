"""Snapshots across many guests at once, VMs and containers alike.

  pvecli snapshot add pre-upgrade --tag web
  pvecli snapshot list --older-than 30d -p '*'
  pvecli snapshot prune --older-than 7d --name-prefix pre- --keep 1

Guests are picked with --vms, --tag, --pool, --node (combinable), or --all.
prune always shows what it would delete and asks before deleting.
"""

import asyncio
import time
from typing import Any

import typer
from rich.table import Table

from ..api.client import ProxmoxClient
from ..api.exceptions import PVECliError
from ..config import ConfigManager
from ..utils import (
    JSON_OPTION,
    confirm,
    console,
    emit_json,
    parse_tags,
    print_cancelled,
    print_error,
    print_info,
    print_success,
    print_warning,
)
from ..utils.fleet import fleet
from ..utils.helpers import async_to_sync, ordered_group
from ..utils.output import err_console
from .find import _parse_age

app = typer.Typer(
    help="Snapshots across many guests: add, list, prune",
    no_args_is_help=True,
    cls=ordered_group(["add", "list", "prune"]),
)

_VMS = typer.Option(None, "--vms", help="Guest IDs: 100,101 or 100-110")
_TAG = typer.Option(None, "--tag", help="Guests carrying this tag")
_POOL = typer.Option(None, "--pool", help="Guests of this resource pool")
_NODE = typer.Option(None, "--node", help="Guests currently on this node")
_ALL = typer.Option(False, "--all", help="Every guest (when no other filter is given)")
_PROFILE = typer.Option(None, "--profile", "-p", help="Profile(s): name, list, pattern ('client-*') or @group")
# Snapshot tasks running at once: each locks its own guest only
_PARALLEL = 4


async def _guests(
    client: ProxmoxClient, vms: str | None, tag: str | None, pool: str | None,
    node: str | None, everything: bool,
) -> list[dict]:
    """Guests matching the filters. Refuses to pick everything implicitly."""
    from ._shared import parse_id_list

    if not any((vms, tag, pool, node)) and not everything:
        raise PVECliError("Pick the guests with --vms, --tag, --pool or --node, or pass --all")
    wanted = set(parse_id_list(vms, "guest")) if vms else None
    out = []
    for g in await client.get_cluster_resources(resource_type="vm"):
        if g.get("type") not in ("qemu", "lxc") or g.get("template"):
            continue
        if wanted is not None and g.get("vmid") not in wanted:
            continue
        if tag and tag not in parse_tags(g.get("tags", "") or ""):
            continue
        if pool and g.get("pool") != pool:
            continue
        if node and g.get("node") != node:
            continue
        out.append(g)
    if not out:
        raise PVECliError("No guest matches these filters")
    return sorted(out, key=lambda g: g["vmid"])


async def _guests_hidden(client: ProxmoxClient) -> bool:
    """True when PVE may leave guests out of /cluster/resources for this user.

    It drops the guests a user may not audit without an error: an empty
    answer then says nothing about them.
    """
    try:
        perms = await client.get("/access/permissions", params={"path": "/vms"}) or {}
    except PVECliError:
        return True
    return not perms.get("/vms", {}).get("VM.Audit")


def _base(g: dict) -> str:
    return f"/nodes/{g['node']}/{g['type']}/{g['vmid']}"


async def _snapshots(client: ProxmoxClient, guests: list[dict]) -> list[dict]:
    """Every snapshot of the guests, newest first per guest."""
    sem = asyncio.Semaphore(16)

    async def one(g: dict) -> list[dict]:
        async with sem:
            try:
                snaps = await client.get(f"{_base(g)}/snapshot") or []
            except PVECliError as e:
                # Said, not swallowed: an empty list here would let `add`
                # miss a duplicate and `list` show a guest without snapshots.
                # On stderr: a --json answer stays parseable
                err_console.print(f"[bold yellow]Warning:[/bold yellow] Snapshots of "
                                  f"{g['vmid']} ({g.get('name', '')}) unreadable: {e}")
                return []
        rows = [
            {"vmid": g["vmid"], "name": g.get("name", ""), "type": "VM" if g["type"] == "qemu" else "CT",
             "node": g["node"], "snapshot": s["name"], "snaptime": s.get("snaptime") or 0,
             "description": (s.get("description") or "").strip(), "vmstate": bool(s.get("vmstate")),
             "_guest": g}
            for s in snaps if s.get("name") != "current"
        ]
        return sorted(rows, key=lambda r: r["snaptime"], reverse=True)

    results = await asyncio.gather(*(one(g) for g in guests))
    return [r for rows in results for r in rows]


def _age(ts: int) -> str:
    if not ts:
        return "?"
    days = (time.time() - ts) / 86400
    return f"{days:.0f}d" if days >= 1 else f"{days * 24:.0f}h"


def _table(rows: list[dict], title: str) -> Table:
    table = Table(title=title, header_style="bold cyan")
    columns: tuple[tuple[str, dict[str, Any]], ...] = (
        ("ID", {"justify": "right"}), ("Name", {}), ("Type", {}), ("Snapshot", {"style": "cyan"}),
        ("Age", {"justify": "right"}), ("Taken", {}), ("RAM", {}), ("Description", {"overflow": "fold"}),
    )
    for col, kw in columns:
        table.add_column(col, **kw)
    for r in rows:
        table.add_row(
            str(r["vmid"]), r["name"], r["type"], r["snapshot"], _age(r["snaptime"]),
            time.strftime("%Y-%m-%d %H:%M", time.localtime(r["snaptime"])) if r["snaptime"] else "-",
            "yes" if r["vmstate"] else "no", r["description"][:60],
        )
    return table


async def _run_tasks(client: ProxmoxClient, jobs: list[tuple[dict, Any]], what: str) -> list[str]:
    """Run snapshot API calls (a few at a time), wait for their tasks. Returns failures."""
    sem = asyncio.Semaphore(_PARALLEL)
    failures: list[str] = []

    async def run(label: dict, call: Any) -> None:
        async with sem:
            try:
                upid = await call()
                if upid:
                    await client.wait_for_task(label["node"], upid, timeout=1800)
            except PVECliError as e:
                failures.append(f"{label['vmid']} ({label.get('name', '')}): {e}")

    await asyncio.gather(*(run(lbl, call) for lbl, call in jobs))
    return failures


@app.command("add")
@fleet
@async_to_sync
async def snapshot_add(
    name: str = typer.Argument(..., help="Snapshot name (letters, digits, - and _)"),
    vms: str = _VMS, tag: str = _TAG, pool: str = _POOL, node: str = _NODE, everything: bool = _ALL,
    description: str = typer.Option(None, "--description", "-d", help="Snapshot description"),
    vmstate: bool = typer.Option(False, "--vmstate", help="Include the RAM of running VMs"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    profile: str = _PROFILE,
) -> None:
    """Take the same snapshot on every selected guest, before a risky change."""
    try:
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            guests = await _guests(client, vms, tag, pool, node, everything)
            existing = {(r["vmid"], r["snapshot"]) for r in await _snapshots(client, guests)}
            clash = [g for g in guests if (g["vmid"], name) in existing]
            if clash:
                print_warning(
                    f"Already have a snapshot '{name}', skipped: "
                    + ", ".join(str(g["vmid"]) for g in clash)
                )
                guests = [g for g in guests if g not in clash]
            if not guests:
                print_info("Nothing to do")
                return
            console.print(
                f"Snapshot '{name}' on {len(guests)} guest(s): "
                + ", ".join(f"{g['vmid']} ({g.get('name', '')})" for g in guests)
            )
            if not yes and not confirm("Take these snapshots?", default=False):
                print_cancelled()
                return
            data: dict[str, Any] = {"snapname": name}
            if description:
                data["description"] = description

            def call(g: dict) -> Any:
                d = dict(data)
                if vmstate and g["type"] == "qemu" and g.get("status") == "running":
                    d["vmstate"] = 1
                return lambda: client.post(f"{_base(g)}/snapshot", data=d)

            failures = await _run_tasks(client, [(g, call(g)) for g in guests], "snapshot")
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    for f in failures:
        print_error(f)
    print_success(f"Snapshot '{name}' taken on {len(guests) - len(failures)}/{len(guests)} guest(s)")
    if failures:
        raise typer.Exit(1)


@app.command("list")
@fleet(parallel=True)
@async_to_sync
async def snapshot_list(
    vms: str = _VMS, tag: str = _TAG, pool: str = _POOL, node: str = _NODE,
    older_than: str = typer.Option(None, "--older-than", help="Only snapshots older than this (30d, 12h, 2w)"),
    name_prefix: str = typer.Option(None, "--name-prefix", help="Only snapshots whose name starts with this"),
    profile: str = _PROFILE,
    json_output: bool = JSON_OPTION,
) -> None:
    """Snapshots of many guests, with their age (every guest when no filter)."""
    try:
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            guests = await _guests(client, vms, tag, pool, node, True)
            rows = await _snapshots(client, guests)
            if await _guests_hidden(client):
                err_console.print("[bold yellow]Warning:[/bold yellow] guests outside your pools "
                                  "are not listed (VM.Audit on /vms missing)")
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    if older_than:
        limit = time.time() - _parse_age(older_than)
        rows = [r for r in rows if r["snaptime"] and r["snaptime"] < limit]
    if name_prefix:
        rows = [r for r in rows if r["snapshot"].startswith(name_prefix)]
    if json_output:
        emit_json([{k: v for k, v in r.items() if k != "_guest"} for r in rows])
        return
    if not rows:
        print_info("No snapshot found")
        return
    console.print(_table(rows, f"{len(rows)} snapshot(s)"))


@app.command("prune")
@fleet
@async_to_sync
async def snapshot_prune(
    older_than: str = typer.Option(..., "--older-than", help="Delete snapshots older than this (7d, 12h, 2w)"),
    name_prefix: str = typer.Option(None, "--name-prefix", help="Only snapshots whose name starts with this (e.g. pre-)"),
    keep: int = typer.Option(0, "--keep", help="Always keep the N most recent matching snapshots of each guest"),
    vms: str = _VMS, tag: str = _TAG, pool: str = _POOL, node: str = _NODE, everything: bool = _ALL,
    yes: bool = typer.Option(False, "--yes", "-y", help="Delete without asking"),
    profile: str = _PROFILE,
) -> None:
    """Delete old snapshots. Shows what would go, deletes only once confirmed."""
    limit = time.time() - _parse_age(older_than)
    try:
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            guests = await _guests(client, vms, tag, pool, node, everything)
            rows = await _snapshots(client, guests)
            doomed: list[dict] = []
            per_guest: dict[int, list[dict]] = {}
            for r in rows:
                if name_prefix and not r["snapshot"].startswith(name_prefix):
                    continue
                per_guest.setdefault(r["vmid"], []).append(r)
            for snaps in per_guest.values():
                # rows come newest first: the first `keep` are spared
                doomed += [s for s in snaps[keep:] if s["snaptime"] and s["snaptime"] < limit]
            if not doomed:
                print_info("Nothing to prune")
                return
            console.print(_table(doomed, f"{len(doomed)} snapshot(s) to delete"))
            if not yes and not confirm(f"Delete these {len(doomed)} snapshot(s)?", default=False):
                print_cancelled()
                return

            # Snapshots of one guest are deleted one after the other (the
            # guest is locked meanwhile), guests in parallel
            async def guest_chain(snaps: list[dict]) -> list[str]:
                errs = []
                for s in sorted(snaps, key=lambda x: x["snaptime"]):
                    g = s["_guest"]
                    errs += await _run_tasks(
                        client,
                        [(g, lambda g=g, s=s: client.delete(f"{_base(g)}/snapshot/{s['snapshot']}"))],
                        "delete",
                    )
                return errs

            by_guest: dict[int, list[dict]] = {}
            for s in doomed:
                by_guest.setdefault(s["vmid"], []).append(s)
            sem = asyncio.Semaphore(_PARALLEL)

            async def limited(snaps: list[dict]) -> list[str]:
                async with sem:
                    return await guest_chain(snaps)

            failures = [f for errs in await asyncio.gather(*(limited(v) for v in by_guest.values())) for f in errs]
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    for f in failures:
        print_error(f)
    print_success(f"{len(doomed) - len(failures)}/{len(doomed)} snapshot(s) deleted")
    if failures:
        raise typer.Exit(1)
