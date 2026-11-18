"""Backups across guests and clusters, through the Proxmox API only.

  pvecli backup status -p '*'
  pvecli backup run --tag web --storage pbs
  pvecli backup restore pbs:backup/vm/100/2026-11-18T02:00:00Z --vmid 900

Backups are found on every enabled storage holding `backup` content: a
shared storage is read once, a local one on each node. Writes always show
what they touch and ask first (-y to skip).
"""

import asyncio
import re
import time
from typing import Any
from urllib.parse import quote

import typer
from rich.table import Table

from ..api.client import ProxmoxClient, is_not_implemented
from ..api.exceptions import APIError, PVECliError
from ..api.exceptions import PermissionError as PVEPermissionError
from ..api.exceptions import TimeoutError as PVETimeoutError
from ..config import ConfigManager
from ..utils import (
    JSON_OPTION,
    confirm,
    console,
    emit_json,
    format_bytes,
    menu_prompt,
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
from .snapshot import _age

app = typer.Typer(
    help="Backups: list, coverage, run, restore, prune, jobs",
    no_args_is_help=True,
    cls=ordered_group(["list", "status", "run", "restore", "remove", "protect", "unprotect",
                       "notes", "prune", "files", "jobs"]),
)
jobs_app = typer.Typer(
    help="Scheduled backup jobs: list, add, edit, remove, enable, disable, run",
    no_args_is_help=True,
    cls=ordered_group(["list", "add", "edit", "remove", "enable", "disable", "run"]),
)
app.add_typer(jobs_app, name="jobs")

_PROFILE = typer.Option(None, "--profile", "-p", help="Profile(s): name, list, pattern ('client-*') or @group")
_PROFILE_ONE = typer.Option(None, "--profile", "-p", help="Profile to use")
_NODE_OPT = typer.Option(None, "--node", help="Node to go through (default: the node holding the backup)")
_YES = typer.Option(False, "--yes", "-y", help="Skip confirmation")
_MODES = ("snapshot", "suspend", "stop")
_COMPRESS = ("0", "gzip", "lzo", "zstd")
# vzdump and restores can take hours; a wait never gives up before that
_TASK_TIMEOUT = 6 * 3600
_CONCURRENCY = 16
# Task logs read per node by `status`: multi-guest runs only say per guest
# outcome in their log, this bounds the cost on a busy node
_MAX_LOGS = 30
_LOG_LINES = 50000
# Keys of a backup job that vzdump does not accept
_JOB_ONLY = {"id", "type", "schedule", "enabled", "comment", "next-run", "node", "starttime", "dow",
             "repeat-missed", "all", "pool", "exclude", "vmid"}
_KEEPS = ("last", "hourly", "daily", "weekly", "monthly", "yearly")
_FILE_TYPES = {"f": "file", "d": "directory", "l": "symlink", "h": "hardlink", "v": "archive",
               "c": "char device", "b": "block device", "p": "fifo", "s": "socket"}

_VZDUMP_NAME = re.compile(r"^backup/(?:.*/)?vzdump-(qemu|lxc|openvz)-\d+-")
_PBS_NAME = re.compile(r"^backup/(vm|ct)/\d+/")
_LOG_START = re.compile(r"Starting Backup of VM (\d+) ")
_LOG_DONE = re.compile(r"Finished Backup of VM (\d+) ")
_LOG_FAIL = re.compile(r"Backup of VM (\d+) failed - (.*)$")
_UNFINISHED = "started but never finished"


# ── pure helpers ─────────────────────────────────────────────────────────


def backup_kind(volid: str) -> str | None:
    """'qemu' or 'lxc' for a backup volid (vzdump file or PBS snapshot), else None."""
    _, sep, path = volid.partition(":")
    if not sep:
        return None
    m = _VZDUMP_NAME.match(path)
    if m:
        return "qemu" if m.group(1) == "qemu" else "lxc"
    m = _PBS_NAME.match(path)
    if m:
        return "qemu" if m.group(1) == "vm" else "lxc"
    return None


def parse_vzdump_log(lines: list[str]) -> dict[int, tuple[str, str]]:
    """Per guest outcome of a vzdump task: vmid -> (ok|failed, reason).

    The task status alone is not enough: a guest that is not on the node is
    dropped without a line, and the task can still end OK. A guest missing
    from the result was not backed up.
    """
    out: dict[int, tuple[str, str]] = {}
    task_error = ""
    for line in lines:
        if m := _LOG_FAIL.search(line):
            out[int(m.group(1))] = ("failed", m.group(2).strip())
        elif m := _LOG_DONE.search(line):
            out[int(m.group(1))] = ("ok", "")
        elif m := _LOG_START.search(line):
            out[int(m.group(1))] = ("failed", _UNFINISHED)
        elif line.startswith("TASK ERROR:"):
            task_error = line.split(":", 1)[1].strip()
    if task_error:
        out = {v: (s, f"{why}: {task_error}" if why == _UNFINISHED else why) for v, (s, why) in out.items()}
    return out


def upid_start(upid: str) -> int:
    """Start time embedded in a UPID (5th field, hex), 0 if unreadable."""
    try:
        return int(upid.split(":")[4], 16)
    except (IndexError, ValueError):
        return 0


def prune_spec(**keeps: int | None) -> str | None:
    """--keep-* options as a prune-backups string, None when none is given."""
    parts = [f"keep-{k}={keeps[k]}" for k in _KEEPS if keeps.get(k) is not None]
    return ",".join(parts) or None


def property_string(value: Any) -> str:
    """A job option read back as an object ({'enabled': 1}) in the form the
    API takes it ('enabled=1')."""
    if isinstance(value, dict):
        return ",".join(f"{k}={v}" for k, v in value.items())
    return str(value)


def _why(err: Exception) -> str:
    """An API error, shortened to the missing privilege when it is one."""
    m = re.search(r"Permission check failed \(([^)]*)\)", str(err))
    return f"missing privilege {m.group(1)}" if m else str(err).strip()


def _when(ts: int | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def _warn(msg: str, json_output: bool) -> None:
    # A --json stdout must stay parseable: advice goes to stderr
    if json_output:
        err_console.print(f"[bold yellow]Warning:[/bold yellow] {msg}")
    else:
        print_warning(msg)


def _enabled(job: dict) -> bool:
    return str(job.get("enabled", 1)).lower() not in ("0", "false", "")


# ── cluster inventory ────────────────────────────────────────────────────


async def _online_nodes(client: ProxmoxClient) -> list[str]:
    return sorted(n["node"] for n in await client.get_nodes() if n.get("status") == "online")


async def _guests(client: ProxmoxClient) -> list[dict]:
    return [g for g in await client.get_cluster_resources(resource_type="vm") or []
            if g.get("type") in ("qemu", "lxc")]


async def _backup_storages(client: ProxmoxClient, only: str | None = None) -> list[dict]:
    """Enabled storages holding backups (one when `only` names it)."""
    storages = [
        s for s in await client.get("/storage") or []
        if "backup" in str(s.get("content", "")).split(",") and not s.get("disable")
    ]
    if only:
        match = [s for s in storages if s["storage"] == only]
        if not match:
            names = ", ".join(s["storage"] for s in storages) or "none"
            raise PVECliError(f"'{only}' is not an enabled backup storage. Backup storages: {names}")
        return match
    return storages


def _allowed_nodes(st: dict, nodes: list[str]) -> list[str]:
    allowed = str(st.get("nodes") or "")
    return [n for n in nodes if not allowed or n in allowed.split(",")]


def _readers(st: dict, nodes: list[str]) -> list[str]:
    """Nodes to read a storage from: one for a shared storage (every node
    sees the same content), each for a local one."""
    allowed = _allowed_nodes(st, nodes)
    return allowed[:1] if st.get("shared") else allowed


def _row(item: dict, st: dict, node: str | None) -> dict[str, Any]:
    volid = item.get("volid", "")
    vmid = item.get("vmid")
    return {
        "volid": volid,
        "storage": st["storage"],
        "node": node,
        "vmid": int(str(vmid)) if str(vmid or "").isdigit() else None,
        "type": item.get("subtype") or backup_kind(volid) or "?",
        "ctime": int(item.get("ctime") or 0),
        "size": int(item.get("size") or item.get("approximate-size") or 0),
        "protected": bool(item.get("protected")),
        "notes": (item.get("notes") or "").strip(),
        "format": item.get("format", ""),
        "verified": (item.get("verification") or {}).get("state"),
    }


async def _inventory(
    client: ProxmoxClient, storages: list[dict], nodes: list[str],
) -> tuple[list[dict], dict[str, str], int]:
    """Every backup on these storages.

    Returns (rows, {'storage' or 'storage@node': error}, number of reads).
    An unreadable storage is returned as such, never as an empty one.
    """
    reads = [(st, n) for st in storages for n in _readers(st, nodes)]
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def read(st: dict, node: str) -> Any:
        async with sem:
            try:
                return await client.get(f"/nodes/{node}/storage/{st['storage']}/content",
                                        params={"content": "backup"}) or []
            except PVECliError as e:
                return e

    results = await asyncio.gather(*(read(st, n) for st, n in reads))
    rows: list[dict] = []
    unreadable: dict[str, str] = {}
    for (st, node), res in zip(reads, results, strict=True):
        shared = bool(st.get("shared"))
        if isinstance(res, Exception):
            unreadable[st["storage"] if shared else f"{st['storage']}@{node}"] = _why(res)
            continue
        rows += [_row(it, st, None if shared else node) for it in res if it.get("content", "backup") == "backup"]
    return rows, unreadable, len(reads)


async def _locate(
    client: ProxmoxClient, volid: str, node: str | None = None,
) -> tuple[str, dict, dict, list[str]]:
    """Where a backup volume can be reached.

    Returns (node, storage config, content item, online nodes). A local
    storage is searched on every node it exists on; a shared one is read
    through `node` or its first online node.
    """
    if backup_kind(volid) is None:
        raise PVECliError(f"'{volid}' is not a backup volume (expected <storage>:backup/...)")
    storage_id = volid.partition(":")[0]
    st = next((s for s in await client.get("/storage") or [] if s["storage"] == storage_id), None)
    if st is None:
        raise PVECliError(f"Storage '{storage_id}' does not exist")
    nodes = await _online_nodes(client)
    candidates = _allowed_nodes(st, nodes)
    if node:
        if node not in candidates:
            raise PVECliError(f"Node '{node}' is not online or has no access to storage '{storage_id}'")
        candidates = [node]
    if not candidates:
        raise PVECliError(f"No online node can reach storage '{storage_id}'")
    if st.get("shared"):
        candidates = candidates[:1]
    hits: list[tuple[str, dict]] = []
    for n in candidates:
        items = await client.get(f"/nodes/{n}/storage/{storage_id}/content", params={"content": "backup"}) or []
        hits += [(n, it) for it in items if it.get("volid") == volid]
    if not hits:
        where = "" if st.get("shared") else f" (searched on {', '.join(candidates)})"
        raise PVECliError(f"Backup '{volid}' not found{where}")
    if len(hits) > 1:
        raise PVECliError(
            f"Backup '{volid}' exists on several nodes ({', '.join(n for n, _ in hits)}): pick one with --node"
        )
    return hits[0][0], st, hits[0][1], nodes


def _volume_path(node: str, volid: str) -> str:
    # The web interface sends the volid encoded too: it holds '/' and ':'
    return f"/nodes/{node}/storage/{volid.partition(':')[0]}/content/{quote(volid, safe='')}"


# ── coverage (status and audit) ──────────────────────────────────────────


async def _job_coverage(client: ProxmoxClient, vmids: list[int]) -> tuple[dict[int, list[str]] | None, str]:
    """vmid -> labels of the jobs covering it ('id' or 'id (disabled)').

    None, with the reason, when no source can be read. When the per job
    guest list is missing (older PVE), falls back on the list of guests in
    no job: the others are covered by '(some job)'.
    """
    first_error = ""
    try:
        jobs = await client.get("/cluster/backup") or []
    except PVECliError as e:
        jobs, first_error = None, _why(e)
    if jobs is not None:
        try:
            included = await asyncio.gather(
                *(client.get(f"/cluster/backup/{j['id']}/included_volumes") for j in jobs)
            )
        except PVECliError as e:
            first_error = _why(e)
        else:
            covered: dict[int, list[str]] = {v: [] for v in vmids}
            for job, tree in zip(jobs, included, strict=True):
                label = job["id"] if _enabled(job) else f"{job['id']} (disabled)"
                for child in (tree or {}).get("children") or []:
                    if str(child.get("id", "")).isdigit():
                        covered.setdefault(int(child["id"]), []).append(label)
            return covered, ""
    try:
        missing = await client.get("/cluster/backup-info/not-backed-up") or []
    except PVECliError as e:
        return None, first_error or _why(e)
    lost = {int(m["vmid"]) for m in missing if str(m.get("vmid", "")).isdigit()}
    return {v: [] if v in lost else ["(some job)"] for v in vmids}, ""


async def _task_log(client: ProxmoxClient, node: str, upid: str) -> list[str] | None:
    try:
        lines = await client.get(f"/nodes/{node}/tasks/{upid}/log", params={"start": 0, "limit": _LOG_LINES})
    except PVECliError:
        return None
    return [str(x.get("t", "")) for x in lines or []]


async def _node_runs(client: ProxmoxClient, node: str) -> dict[int, dict] | str:
    """Last finished vzdump outcome per guest seen on a node, or why it
    cannot be known (PVE silently hides other users' tasks without
    Sys.Audit on the node: an empty list would read as 'never ran')."""
    try:
        perms = await client.get("/access/permissions", params={"path": f"/nodes/{node}"}) or {}
    except PVECliError as e:
        return _why(e)
    if not (perms.get(f"/nodes/{node}") or {}).get("Sys.Audit"):
        return f"missing privilege /nodes/{node}, Sys.Audit"
    try:
        tasks = await client.get(f"/nodes/{node}/tasks", params={"typefilter": "vzdump", "limit": 100}) or []
    except PVECliError as e:
        return _why(e)
    runs: dict[int, dict] = {}
    logs = 0
    for t in sorted(tasks, key=lambda x: x.get("starttime") or 0, reverse=True):
        status = str(t.get("status") or "")
        if not t.get("endtime") or not status:
            continue  # still running
        when = int(t.get("starttime") or 0)
        guest = str(t.get("id") or "")
        if guest.isdigit():
            if int(guest) in runs:
                continue
            if status == "OK":
                runs[int(guest)] = {"state": "ok", "time": when, "detail": ""}
                continue
        if logs >= _MAX_LOGS:
            continue
        logs += 1
        lines = await _task_log(client, node, t["upid"])
        parsed = parse_vzdump_log(lines or [])
        if guest.isdigit() and int(guest) not in parsed:
            parsed[int(guest)] = ("failed", status)
        for vmid, (state, why) in parsed.items():
            if vmid not in runs:
                runs[vmid] = {"state": state, "time": when, "detail": why}
    return runs


async def collect_coverage(client: ProxmoxClient, guests: list[dict], nodes: list[str]) -> dict[str, Any]:
    """Everything `status` and the audit need, each source read once.

    Keys: jobs (vmid -> labels, or None), jobs_error, last (vmid -> newest
    backup row), unreadable (storage -> error), runs (vmid -> last run),
    blind (node -> why its tasks cannot be read).
    """
    storages = await _backup_storages(client)
    (jobs, jobs_error), (rows, unreadable, _), node_runs = await asyncio.gather(
        _job_coverage(client, [g["vmid"] for g in guests]),
        _inventory(client, storages, nodes),
        asyncio.gather(*(_node_runs(client, n) for n in nodes)),
    )
    last: dict[int, dict] = {}
    for r in rows:
        if r["vmid"] is not None and r["ctime"] > last.get(r["vmid"], {}).get("ctime", -1):
            last[r["vmid"]] = r
    runs: dict[int, dict] = {}
    blind: dict[str, str] = {}
    for node, res in zip(nodes, node_runs, strict=True):
        if isinstance(res, str):
            blind[node] = res
            continue
        for vmid, run in res.items():
            if run["time"] >= runs.get(vmid, {}).get("time", -1):
                runs[vmid] = run
    return {"jobs": jobs, "jobs_error": jobs_error, "last": last, "unreadable": unreadable,
            "runs": runs, "blind": blind}


def _status_rows(guests: list[dict], cov: dict[str, Any]) -> list[dict]:
    rows = []
    for g in sorted(guests, key=lambda x: x["vmid"]):
        vmid = g["vmid"]
        last = cov["last"].get(vmid)
        if g.get("node") in cov["blind"]:
            run = {"state": "not checked", "time": None, "detail": cov["blind"][g["node"]]}
        else:
            run = cov["runs"].get(vmid) or {"state": "none", "time": None, "detail": ""}
        jobs = cov["jobs"]
        rows.append({
            "vmid": vmid, "name": g.get("name", ""), "type": g["type"], "node": g.get("node"),
            "template": bool(g.get("template")),
            "jobs": None if jobs is None else jobs.get(vmid, []),
            "jobs_detail": cov["jobs_error"] if jobs is None else "",
            "last_backup": last["ctime"] if last else None,
            "last_volid": last["volid"] if last else None,
            "last_run": run,
        })
    return rows


# ── list ─────────────────────────────────────────────────────────────────


@app.command("list")
@fleet(parallel=True)
@async_to_sync
async def backup_list(
    vmid: str = typer.Option(None, "--vmid", help="Only these guests: 100,101 or 100-110"),
    tag: str = typer.Option(None, "--tag", help="Only guests carrying this tag"),
    pool: str = typer.Option(None, "--pool", help="Only guests of this resource pool"),
    storage: str = typer.Option(None, "--storage", help="Only this backup storage"),
    profile: str = _PROFILE,
    json_output: bool = JSON_OPTION,
) -> None:
    """Backups of every guest, on every backup storage (newest first).

    Examples:
        pvecli backup list
        pvecli backup list --vmid 100 --storage pbs
        pvecli backup list -p '*' --json
    """
    from ._shared import parse_id_list

    try:
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            nodes = await _online_nodes(client)
            storages = await _backup_storages(client, storage)
            guests = await _guests(client)
            rows, unreadable, reads = await _inventory(client, storages, nodes)
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    for label, why in unreadable.items():
        _warn(f"Backups on {label} unreadable: {why}", json_output)
    if reads and len(unreadable) == reads:
        print_error("No backup storage could be read")
        raise typer.Exit(1)

    by_id = {g["vmid"]: g for g in guests}
    wanted: set[int] | None = set(parse_id_list(vmid, "guest")) if vmid else None
    if tag or pool:
        picked = {g["vmid"] for g in guests
                  if (not tag or tag in parse_tags(g.get("tags", "") or ""))
                  and (not pool or g.get("pool") == pool)}
        wanted = picked if wanted is None else wanted & picked
    if wanted is not None:
        rows = [r for r in rows if r["vmid"] in wanted]
    for r in rows:
        r["name"] = by_id.get(r["vmid"], {}).get("name", "")
    rows.sort(key=lambda r: (r["vmid"] or 0, -r["ctime"]))

    if json_output:
        emit_json(rows)
        return
    if not rows:
        print_info("No backup found")
        return
    table = Table(title=f"{len(rows)} backup(s)", header_style="bold cyan")
    columns: tuple[tuple[str, dict[str, Any]], ...] = (
        ("Volid", {"overflow": "fold"}), ("ID", {"justify": "right"}), ("Name", {}),
        ("Age", {"justify": "right"}), ("Size", {"justify": "right"}), ("Protected", {}),
        ("Notes", {"overflow": "fold"}),
    )
    for col, kw in columns:
        table.add_column(col, **kw)
    for r in rows:
        table.add_row(
            r["volid"], str(r["vmid"] or "?"), r["name"] or "[dim]gone[/dim]", _age(r["ctime"]),
            format_bytes(r["size"]) if r["size"] else "-", "yes" if r["protected"] else "", r["notes"][:60],
        )
    console.print(table)


# ── status ───────────────────────────────────────────────────────────────


@app.command("status")
@fleet(parallel=True)
@async_to_sync
async def backup_status(
    older_than: str = typer.Option(None, "--older-than", help="Only guests whose last backup is older (2d, 12h, 1w) or missing"),
    failed: bool = typer.Option(False, "--failed", help="Only guests whose last backup run failed"),
    profile: str = _PROFILE,
    json_output: bool = JSON_OPTION,
) -> None:
    """Backup coverage of each guest: covering job, last backup, last run.

    With --older-than and --failed together, guests with either problem
    are shown.

    Examples:
        pvecli backup status
        pvecli backup status --older-than 2d --failed -p '*'
    """
    limit = time.time() - _parse_age(older_than) if older_than else None
    try:
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            nodes = await _online_nodes(client)
            guests = await _guests(client)
            cov = await collect_coverage(client, guests, nodes)
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    if cov["jobs"] is None:
        _warn(f"Backup jobs not checked: {cov['jobs_error']}", json_output)
    for label, why in cov["unreadable"].items():
        _warn(f"Backups on {label} unreadable, last backup dates may be older than real: {why}", json_output)
    for node, why in cov["blind"].items():
        _warn(f"Backup runs on {node} not checked: {why}", json_output)

    rows = _status_rows(guests, cov)
    if limit is not None or failed:
        rows = [
            r for r in rows
            if (failed and r["last_run"]["state"] == "failed")
            or (limit is not None and (r["last_backup"] is None or r["last_backup"] < limit))
        ]
    if json_output:
        emit_json(rows)
        return
    if not rows:
        print_info("No guest matches" if (limit or failed) else "No guest")
        return
    table = Table(title=f"Backup status of {len(rows)} guest(s)", header_style="bold cyan")
    columns: tuple[tuple[str, dict[str, Any]], ...] = (
        ("ID", {"justify": "right"}), ("Name", {}), ("Type", {}), ("Job", {}),
        ("Last backup", {"justify": "right"}), ("Last run", {}), ("Detail", {"overflow": "fold"}),
    )
    for col, kw in columns:
        table.add_column(col, **kw)
    for r in rows:
        if r["jobs"] is None:
            jobs = "[yellow]not checked[/yellow]"
        else:
            jobs = ", ".join(r["jobs"]) or "[yellow]no job[/yellow]"
        run = r["last_run"]
        state = {
            "ok": f"[green]OK[/green] {_age(run['time'])}",
            "failed": f"[red]FAILED[/red] {_age(run['time'])}",
            "not checked": "[yellow]not checked[/yellow]",
        }.get(run["state"], "-")
        table.add_row(
            str(r["vmid"]), r["name"], "VM" if r["type"] == "qemu" else "CT", jobs,
            _age(r["last_backup"]) if r["last_backup"] else "[yellow]never[/yellow]",
            state, run["detail"],
        )
    console.print(table)


# ── run (and jobs run) ───────────────────────────────────────────────────


async def _targets(
    client: ProxmoxClient, ids: str | None, tag: str | None, pool: str | None,
    node: str | None, everything: bool,
) -> list[dict]:
    """Guests to back up. Explicit IDs keep templates; filters skip them."""
    from ._shared import parse_id_list

    if not any((ids, tag, pool, node)) and not everything:
        raise PVECliError("Pick the guests: IDs (100,101 or 100-110), --tag, --pool or --node, or pass --all")
    guests = await _guests(client)
    wanted = set(parse_id_list(ids, "guest")) if ids else None
    if wanted:
        unknown = sorted(wanted - {g["vmid"] for g in guests})
        if unknown:
            raise PVECliError(f"Guest(s) not found: {', '.join(map(str, unknown))}")
    out = []
    for g in guests:
        if wanted is not None:
            if g["vmid"] not in wanted:
                continue
        elif g.get("template"):
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


def _job_id_refused(err: Exception) -> bool:
    # job-id is root@pam only, and unknown to older versions
    return isinstance(err, PVEPermissionError) or is_not_implemented(err) or "job-id" in str(err)


async def _vzdump(
    client: ProxmoxClient, guests: list[dict], params: dict[str, dict[str, Any]], job_id: str | None = None,
) -> list[dict]:
    """One vzdump per node, all nodes at once; per guest result, checked twice.

    params maps each node to its vzdump options. A guest is backed up only
    when its log says so AND a new archive of it is on the storage: a task
    can end OK and still have skipped a guest.
    """
    by_node: dict[str, list[dict]] = {}
    for g in guests:
        by_node.setdefault(g["node"], []).append(g)
    refused_job_id: list[str] = []

    async def on_node(node: str, members: list[dict]) -> tuple[dict[int, tuple[str, str]], int]:
        data = {**params[node], "vmid": ",".join(str(g["vmid"]) for g in members)}
        if job_id:
            data["job-id"] = job_id
        try:
            try:
                upid = await client.post(f"/nodes/{node}/vzdump", data=data)
            except PVECliError as e:
                if not (job_id and _job_id_refused(e)):
                    raise
                refused_job_id.append(_why(e))
                data = {k: v for k, v in data.items() if k != "job-id"}
                upid = await client.post(f"/nodes/{node}/vzdump", data=data)
        except PVECliError as e:
            return {g["vmid"]: ("failed", _why(e)) for g in members}, 0
        if not (isinstance(upid, str) and upid.startswith("UPID:")):
            # Seen on 9.1: every guest asked had left the node, PVE answers
            # "OK" and starts nothing
            return {g["vmid"]: ("failed", f"vzdump on {node} started no task (answered {upid!r}): "
                                "the guest may have moved to another node") for g in members}, 0
        task_error = ""
        try:
            await client.wait_for_task(node, upid, timeout=_TASK_TIMEOUT)
        except PVETimeoutError:
            return {g["vmid"]: ("failed", f"still running after {_TASK_TIMEOUT // 3600}h ({upid})")
                    for g in members}, 0
        except PVECliError as e:
            task_error = str(e)  # per guest detail is in the log
        lines = await _task_log(client, node, upid)
        parsed = parse_vzdump_log(lines) if lines is not None else {}
        out: dict[int, tuple[str, str]] = {}
        for g in members:
            # vzdump drops a guest that is not on its node without a word:
            # absent from the log means not saved
            state, why = parsed.get(g["vmid"], ("failed", "absent from the task log, not backed up"))
            if lines is None:
                state, why = ("ok", "") if not task_error else ("failed", task_error)
            out[g["vmid"]] = (state, why)
        return out, upid_start(upid)

    nodes = sorted(by_node)
    results = await asyncio.gather(*(on_node(n, by_node[n]) for n in nodes))
    if refused_job_id:
        print_warning(f"job-id refused ({refused_job_id[0]}): ran as a plain backup")

    # Second check: the archive really is on the storage
    storage_cfg = {s["storage"]: s for s in await client.get("/storage") or []}
    reads: dict[tuple[str, str], Any] = {}
    for node, (outcome, _) in zip(nodes, results, strict=True):
        st = storage_cfg.get(str(params[node].get("storage", "")))
        if st and any(s == "ok" for s, _ in outcome.values()):
            reads[(st["storage"], "" if st.get("shared") else node)] = (st, node)

    async def read(st: dict, node: str) -> Any:
        try:
            return await client.get(f"/nodes/{node}/storage/{st['storage']}/content",
                                    params={"content": "backup"}) or []
        except PVECliError as e:
            return e

    listed = dict(zip(reads, await asyncio.gather(*(read(*v) for v in reads.values())), strict=True))
    rows = []
    for node, (outcome, start) in zip(nodes, results, strict=True):
        st = storage_cfg.get(str(params[node].get("storage", "")))
        for g in by_node[node]:
            state, why = outcome[g["vmid"]]
            volid = ""
            if state == "ok" and st:
                items = listed.get((st["storage"], "" if st.get("shared") else node))
                if isinstance(items, Exception):
                    why = f"archive not verified: {_why(items)}"
                else:
                    fresh = [i for i in items or [] if str(i.get("vmid")) == str(g["vmid"])
                             and int(i.get("ctime") or 0) >= start - 5]
                    if fresh:
                        volid = max(fresh, key=lambda i: int(i.get("ctime") or 0))["volid"]
                    else:
                        state, why = "failed", f"vzdump reported success but no new archive is on {st['storage']}"
            rows.append({"vmid": g["vmid"], "name": g.get("name", ""), "node": node,
                         "state": state, "detail": why, "volid": volid})
    return sorted(rows, key=lambda r: r["vmid"])


def _report(rows: list[dict]) -> None:
    table = Table(header_style="bold cyan")
    for col in ("ID", "Name", "Node", "Result", "Archive or reason"):
        table.add_column(col, overflow="fold")
    for r in rows:
        ok = r["state"] == "ok"
        table.add_row(str(r["vmid"]), r["name"], r["node"], "[green]OK[/green]" if ok else "[red]FAILED[/red]",
                      r["volid"] or r["detail"])
    console.print(table)
    done = sum(r["state"] == "ok" for r in rows)
    for r in rows:
        if r["state"] != "ok":
            print_error(f"{r['vmid']} ({r['name']}): {r['detail']}")
    msg = f"{done}/{len(rows)} guest(s) backed up"
    if done == len(rows):
        print_success(msg)
    else:
        print_warning(msg)
        raise typer.Exit(1)


@app.command("run")
@fleet
@async_to_sync
async def backup_run(
    ids: str = typer.Argument(None, help="Guest IDs: 100,101 or 100-110"),
    tag: str = typer.Option(None, "--tag", help="Guests carrying this tag"),
    pool: str = typer.Option(None, "--pool", help="Guests of this resource pool"),
    node: str = typer.Option(None, "--node", help="Guests currently on this node"),
    everything: bool = typer.Option(False, "--all", help="Every guest except templates"),
    storage: str = typer.Option(None, "--storage", "-s", help="Backup storage (default: each node's vzdump default)"),
    mode: str = typer.Option("snapshot", "--mode", help="snapshot, suspend or stop"),
    compress: str = typer.Option("zstd", "--compress", help="zstd, gzip, lzo or 0 (none; ignored by PBS)"),
    notes: str = typer.Option(None, "--notes", help="Notes of the backups ({{guestname}}, {{vmid}}, {{node}} expand)"),
    protected: bool = typer.Option(False, "--protected", help="Protect the backups from pruning and removal"),
    yes: bool = _YES,
    profile: str = _PROFILE,
) -> None:
    """Back up guests now (one vzdump per node), then check each guest.

    Older backups are never pruned by this run (unlike PVE's default).
    Exit code 1 when a guest was not backed up.

    Examples:
        pvecli backup run 100,101 --storage pbs
        pvecli backup run --tag web --mode stop --protected -y
    """
    if mode not in _MODES:
        print_error(f"--mode must be one of: {', '.join(_MODES)}")
        raise typer.Exit(1)
    if compress not in _COMPRESS:
        print_error(f"--compress must be one of: {', '.join(_COMPRESS)}")
        raise typer.Exit(1)
    try:
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            guests = await _targets(client, ids, tag, pool, node, everything)
            nodes = sorted({g["node"] for g in guests})
            backup_storages = {s["storage"]: s for s in await _backup_storages(client)}
            targets: dict[str, str] = {}
            for n in nodes:
                # Explicit even when defaulted: protected and notes need it,
                # and the result is checked on it
                name = storage or (await client.get(f"/nodes/{n}/vzdump/defaults") or {}).get("storage") or "local"
                st = backup_storages.get(name)
                if st is None:
                    raise PVECliError(
                        f"'{name}' is not an enabled backup storage. Backup storages: "
                        + (", ".join(backup_storages) or "none")
                    )
                if n not in _allowed_nodes(st, [n]):
                    raise PVECliError(f"Storage '{name}' is not available on node {n}")
                targets[n] = name
            params: dict[str, dict[str, Any]] = {}
            for n in nodes:
                # remove=0: PVE prunes by the storage retention by default
                p: dict[str, Any] = {"storage": targets[n], "mode": mode, "compress": compress, "remove": 0}
                if notes:
                    p["notes-template"] = notes
                if protected:
                    p["protected"] = 1
                params[n] = p
            where = ", ".join(sorted(set(targets.values())))
            console.print(
                f"Back up {len(guests)} guest(s) to {where} (mode {mode}): "
                + ", ".join(f"{g['vmid']} ({g.get('name', '')})" for g in guests)
            )
            if not yes and not confirm(f"Back up these {len(guests)} guest(s)?", default=False):
                print_cancelled()
                return
            with console.status(f"Backing up {len(guests)} guest(s)..."):
                rows = await _vzdump(client, guests, params)
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    _report(rows)


# ── restore ──────────────────────────────────────────────────────────────


def _rootfs_storage(config: str) -> str | None:
    for line in str(config).splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "rootfs":
            vol = value.strip().split(",")[0]
            if ":" in vol:
                return vol.split(":")[0]
    return None


@app.command("restore")
@async_to_sync
async def backup_restore(
    volid: str = typer.Argument(..., help="Backup volume, e.g. pbs:backup/vm/100/2026-11-18T02:00:00Z"),
    vmid: int = typer.Option(None, "--vmid", help="Target ID (default: next free ID)"),
    node: str = typer.Option(None, "--node", help="Node to restore on (default: where the backup or guest is)"),
    storage: str = typer.Option(None, "--storage", "-s",
                                help="Target storage (VM default: from the backup; CT default: storage of its root disk)"),
    start: bool = typer.Option(False, "--start", help="Start the guest once restored"),
    unique: bool = typer.Option(False, "--unique", help="New random MAC addresses (the original may still run)"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing, stopped guest with this ID"),
    yes: bool = _YES,
    profile: str = _PROFILE_ONE,
) -> None:
    """Restore a backup as a new guest, or over a stopped one with --force.

    Examples:
        pvecli backup restore local:backup/vzdump-qemu-100-2026_11_18-02_00_00.vma.zst
        pvecli backup restore pbs:backup/ct/200/2026-11-18T02:00:00Z --vmid 905 --start
    """
    try:
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            # A local backup must be on --node; a shared one is read through it
            holder, st, item, nodes = await _locate(client, volid, node)
            kind = item.get("subtype") or backup_kind(volid)
            kind = "lxc" if kind in ("lxc", "openvz") else "qemu"
            label = "VM" if kind == "qemu" else "CT"
            guests = {g["vmid"]: g for g in await _guests(client)}

            existing = None
            if vmid is None:
                target = await client.get_next_vmid()
            else:
                target = vmid
                try:
                    await client.get("/cluster/nextid", params={"vmid": vmid})
                except APIError as e:
                    if "exists" not in str(e):
                        raise
                    existing = guests.get(vmid) or {"vmid": vmid}

            if existing is not None:
                if not force:
                    raise PVECliError(
                        f"Guest {target} already exists. Pick a free ID with --vmid, "
                        "or overwrite it with --force (it must be stopped)"
                    )
                if "node" not in existing:
                    raise PVECliError(f"Guest {target} exists but is not visible to this user")
                if existing["type"] != kind:
                    what = "a container" if existing["type"] == "lxc" else "a VM"
                    raise PVECliError(f"Guest {target} is {what}, this backup is a {label}: not overwriting it")
                state = (await client.get(
                    f"/nodes/{existing['node']}/{existing['type']}/{target}/status/current") or {}).get("status")
                if state != "stopped":
                    raise PVECliError(f"Guest {target} is {state}: stop it first, then restore")

            # Where the restore runs
            if existing is not None:
                where = existing["node"]
                if node and node != where:
                    raise PVECliError(f"Guest {target} lives on {where}: the restore has to run there")
                if not st.get("shared") and holder != where:
                    raise PVECliError(
                        f"Guest {target} lives on {where} but this backup is on {st['storage']} of {holder}: "
                        f"{where} cannot read it"
                    )
            elif st.get("shared"):
                original = guests.get(item.get("vmid"))
                where = node or (original["node"] if original and original.get("node") in nodes else holder)
                if where not in _allowed_nodes(st, nodes):
                    raise PVECliError(f"Node '{where}' is not online or has no access to storage '{st['storage']}'")
            else:
                where = holder

            data: dict[str, Any] = {"vmid": target}
            if kind == "qemu":
                data["archive"] = volid
            else:
                data["ostemplate"] = volid
                data["restore"] = 1
            target_storage: str | None = storage
            if kind == "lxc" and not storage:
                # PVE would put every volume on 'local' when storage is omitted
                config = await client.get(f"/nodes/{holder}/vzdump/extractconfig", params={"volume": volid})
                target_storage = _rootfs_storage(config)
                if not target_storage:
                    raise PVECliError("Could not read the root disk storage from the backup: pass --storage")
            if target_storage:
                data["storage"] = target_storage
            if start:
                data["start"] = 1
            if unique:
                data["unique"] = 1

            console.print(f"Restore [cyan]{volid}[/cyan]")
            console.print(f"  as {label} {target} on {where}, storage: "
                          + (target_storage or "as in the backup"))
            if existing is not None:
                print_warning(
                    f"This overwrites guest {target} ({existing.get('name', '')}): "
                    "its current disks and configuration are destroyed"
                )
                if not yes:
                    typed = menu_prompt(f"  Type the VMID {target} again to confirm")
                    if str(typed).strip() != str(target):
                        print_error("VMID does not match")
                        print_cancelled()
                        raise typer.Exit(1)
                data["force"] = 1
            elif not yes and not confirm("Restore it?", default=False):
                print_cancelled()
                return

            path = f"/nodes/{where}/{kind}"
            with console.status(f"Restoring {label} {target}..."):
                upid = await client.post(path, data=data)
                if upid:
                    await client.wait_for_task(where, upid, timeout=_TASK_TIMEOUT)
            status = (await client.get(f"{path}/{target}/status/current") or {}).get("status", "?")
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    print_success(f"Restored as {label} {target} on {where} ({status})")
    if start and status != "running":
        print_warning(f"{label} {target} was asked to start but is {status}")


# ── remove, protect, unprotect, notes ────────────────────────────────────


async def _locate_many(client: ProxmoxClient, volids: list[str], node: str | None) -> tuple[list[tuple[str, str, dict]], list[str]]:
    found: list[tuple[str, str, dict]] = []
    errors: list[str] = []
    for v in volids:
        try:
            n, _, item, _ = await _locate(client, v, node)
            found.append((v, n, item))
        except PVECliError as e:
            errors.append(str(e))
    return found, errors


@app.command("remove")
@async_to_sync
async def backup_remove(
    volids: list[str] = typer.Argument(..., help="Backup volume(s) to delete"),
    node: str = _NODE_OPT,
    yes: bool = _YES,
    profile: str = _PROFILE_ONE,
) -> None:
    """Delete backups. A protected backup is never deleted (unprotect it first)."""
    failures: list[str] = []
    try:
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            found, failures = await _locate_many(client, volids, node)
            doomed = []
            for v, n, item in found:
                if item.get("protected"):
                    failures.append(f"{v} is protected: unprotect it first with pvecli backup unprotect {v}")
                else:
                    doomed.append((v, n))
            for f in failures:
                print_error(f)
            if doomed:
                console.print("[bold red]Delete backup(s):[/bold red]")
                for v, n in doomed:
                    console.print(f"  {v}  [dim]({n})[/dim]")
                if not yes and not confirm(f"Delete these {len(doomed)} backup(s)?", default=False):
                    print_cancelled()
                    return
            done = 0
            for v, n in doomed:
                try:
                    upid = await client.delete(_volume_path(n, v))
                    if isinstance(upid, str) and upid.startswith("UPID:"):
                        await client.wait_for_task(n, upid, timeout=_TASK_TIMEOUT)
                    done += 1
                except PVECliError as e:
                    msg = f"{v}: {e}"
                    print_error(msg)
                    failures.append(msg)
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    if doomed:
        print_success(f"{done}/{len(doomed)} backup(s) deleted")
    if failures:
        raise typer.Exit(1)


async def _update_volumes(volids: list[str], node: str | None, profile: str | None,
                          data: dict[str, Any], what: str) -> None:
    failures: list[str] = []
    try:
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            found, failures = await _locate_many(client, volids, node)
            for v, n, _ in found:
                try:
                    await client.put(_volume_path(n, v), data=data)
                    print_success(f"{v} {what}")
                except PVECliError as e:
                    failures.append(f"{v}: {e}")
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    for f in failures:
        print_error(f)
    if failures:
        raise typer.Exit(1)


@app.command("protect")
@async_to_sync
async def backup_protect(
    volids: list[str] = typer.Argument(..., help="Backup volume(s)"),
    node: str = _NODE_OPT,
    profile: str = _PROFILE_ONE,
) -> None:
    """Protect backups: neither pruning nor removal will delete them."""
    await _update_volumes(volids, node, profile, {"protected": 1}, "protected")


@app.command("unprotect")
@async_to_sync
async def backup_unprotect(
    volids: list[str] = typer.Argument(..., help="Backup volume(s)"),
    node: str = _NODE_OPT,
    profile: str = _PROFILE_ONE,
) -> None:
    """Remove the protection of backups."""
    await _update_volumes(volids, node, profile, {"protected": 0}, "unprotected")


@app.command("notes")
@async_to_sync
async def backup_notes(
    volid: str = typer.Argument(..., help="Backup volume"),
    text: str = typer.Argument(..., help="New notes (empty string to clear)"),
    node: str = _NODE_OPT,
    profile: str = _PROFILE_ONE,
) -> None:
    """Set the notes of a backup."""
    await _update_volumes([volid], node, profile, {"notes": text}, "notes updated")


# ── prune ────────────────────────────────────────────────────────────────


@app.command("prune")
@async_to_sync
async def backup_prune(
    storage: str = typer.Argument(..., help="Backup storage to prune"),
    keep_last: int = typer.Option(None, "--keep-last", min=0, help="Keep the N newest backups"),
    keep_hourly: int = typer.Option(None, "--keep-hourly", min=0, help="Keep one backup per hour, N hours"),
    keep_daily: int = typer.Option(None, "--keep-daily", min=0, help="Keep one backup per day, N days"),
    keep_weekly: int = typer.Option(None, "--keep-weekly", min=0, help="Keep one backup per week, N weeks"),
    keep_monthly: int = typer.Option(None, "--keep-monthly", min=0, help="Keep one backup per month, N months"),
    keep_yearly: int = typer.Option(None, "--keep-yearly", min=0, help="Keep one backup per year, N years"),
    vmid: int = typer.Option(None, "--vmid", help="Only the backups of this guest"),
    guest_type: str = typer.Option(None, "--type", help="Only qemu or lxc backups"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be removed, remove nothing"),
    yes: bool = _YES,
    profile: str = _PROFILE_ONE,
) -> None:
    """Prune backups by retention: preview from PVE first, then remove once confirmed.

    Without any --keep-* option, the retention configured on the storage
    applies. Protected backups are always kept.

    Examples:
        pvecli backup prune local --keep-last 3 --keep-daily 7 --dry-run
        pvecli backup prune pbs --keep-last 1 --vmid 100
    """
    if guest_type and guest_type not in ("qemu", "lxc"):
        print_error("--type must be qemu or lxc")
        raise typer.Exit(1)
    params: dict[str, Any] = {}
    spec = prune_spec(last=keep_last, hourly=keep_hourly, daily=keep_daily, weekly=keep_weekly,
                      monthly=keep_monthly, yearly=keep_yearly)
    if spec:
        params["prune-backups"] = spec
    if vmid is not None:
        params["vmid"] = vmid
    if guest_type:
        params["type"] = guest_type
    failures: list[str] = []
    try:
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            [st] = await _backup_storages(client, storage)
            nodes = _readers(st, await _online_nodes(client))
            if not nodes:
                raise PVECliError(f"No online node can reach storage '{storage}'")
            plans: dict[str, list[dict]] = {}
            for n in nodes:
                try:
                    plans[n] = await client.get(f"/nodes/{n}/storage/{storage}/prunebackups",
                                                params=params or None) or []
                except APIError as e:
                    if "no prune-backups options configured" in str(e):
                        raise PVECliError(
                            f"Storage '{storage}' has no retention configured: pass --keep-last, "
                            "--keep-daily, --keep-weekly, --keep-monthly or --keep-yearly"
                        ) from e
                    raise
            if not spec:
                print_info(f"Using the retention of the storage '{storage}'")
            table = Table(header_style="bold cyan")
            # A local storage has one content per node: say which
            per_node = not st.get("shared")
            for col in ("Volid", *(("Node",) if per_node else ()), "ID", "Type", "Age", "Decision"):
                table.add_column(col, overflow="fold")
            colors = {"remove": "red", "keep": "green", "protected": "yellow"}
            doomed: dict[str, int] = {}
            for n, plan in plans.items():
                for item in sorted(plan, key=lambda x: (x.get("vmid") or 0, -(x.get("ctime") or 0))):
                    mark = item.get("mark", "?")
                    c = colors.get(mark, "white")
                    table.add_row(item.get("volid", ""), *((n,) if per_node else ()), str(item.get("vmid", "")),
                                  item.get("type", ""), _age(item.get("ctime") or 0), f"[{c}]{mark}[/{c}]")
                    if mark == "remove":
                        doomed[n] = doomed.get(n, 0) + 1
            total = sum(doomed.values())
            if not total:
                print_info("Nothing to prune")
                return
            console.print(table)
            console.print(f"{total} backup(s) to remove")
            if dry_run:
                print_info("Dry run: nothing removed")
                return
            if not yes and not confirm(f"Remove these {total} backup(s)?", default=False):
                print_cancelled()
                return
            for n in doomed:
                try:
                    upid = await client.delete(f"/nodes/{n}/storage/{storage}/prunebackups", params=params or None)
                    if isinstance(upid, str) and upid.startswith("UPID:"):
                        await client.wait_for_task(n, upid, timeout=_TASK_TIMEOUT)
                except PVECliError as e:
                    failures.append(f"{n}: {e}")
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    for f in failures:
        print_error(f)
    if failures:
        raise typer.Exit(1)
    print_success(f"{total} backup(s) pruned from {storage}")


# ── files (PBS) ──────────────────────────────────────────────────────────


@app.command("files")
@async_to_sync
async def backup_files(
    volid: str = typer.Argument(..., help="PBS backup volume"),
    path: str = typer.Argument("/", help="Path inside the backup, e.g. /root.pxar.didx/etc"),
    node: str = _NODE_OPT,
    profile: str = _PROFILE_ONE,
    json_output: bool = JSON_OPTION,
) -> None:
    """Browse the files of a Proxmox Backup Server backup (read only).

    Examples:
        pvecli backup files pbs:backup/ct/200/2026-11-18T02:00:00Z
        pvecli backup files pbs:backup/ct/200/2026-11-18T02:00:00Z /root.pxar.didx/etc
    """
    try:
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            n, st, _, _ = await _locate(client, volid, node)
            if st.get("type") != "pbs":
                raise PVECliError(
                    f"'{st['storage']}' is a {st.get('type')} storage: browsing files needs a "
                    "Proxmox Backup Server storage (this backup can only be restored whole)"
                )
            url = f"/nodes/{n}/storage/{st['storage']}/file-restore/list"
            entries = await client.get(url, params={"volume": volid, "filepath": "/"}) or []
            walked = ""
            # Follow the ids the API hands out rather than encoding paths ourselves
            for part in [p for p in path.split("/") if p]:
                match = next((e for e in entries if e.get("text") == part), None)
                if match is None:
                    names = ", ".join(str(e.get("text")) for e in entries) or "nothing"
                    raise PVECliError(f"'{part}' not found in /{walked}. It holds: {names}")
                walked = f"{walked}/{part}".lstrip("/")
                if match.get("leaf"):
                    entries = [match]
                    break
                entries = await client.get(url, params={"volume": volid, "filepath": match["filepath"]}) or []
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    rows = [{"name": e.get("text", ""), "type": _FILE_TYPES.get(str(e.get("type")), str(e.get("type"))),
             "size": e.get("size"), "mtime": e.get("mtime")} for e in entries]
    if json_output:
        emit_json(rows)
        return
    if not rows:
        print_info(f"Empty: nothing to list at /{walked}")
        return
    table = Table(title=f"{volid} /{walked}", header_style="bold cyan")
    for col in ("Name", "Type", "Size", "Modified"):
        table.add_column(col)
    for r in rows:
        table.add_row(r["name"], r["type"], format_bytes(r["size"]) if r["size"] is not None else "",
                      _when(r["mtime"]))
    console.print(table)


# ── jobs ─────────────────────────────────────────────────────────────────


def _selection(job: dict) -> str:
    if job.get("all") and str(job["all"]) not in ("0", "false"):
        return "all" + (f" except {job['exclude']}" if job.get("exclude") else "")
    if job.get("pool"):
        return f"pool {job['pool']}"
    return str(job.get("vmid", "-"))


async def _get_job(client: ProxmoxClient, job_id: str) -> dict:
    try:
        return await client.get(f"/cluster/backup/{job_id}") or {}
    except PVECliError as e:
        raise PVECliError(f"Backup job '{job_id}' not found: {e}") from e


@jobs_app.command("list")
@fleet(parallel=True)
@async_to_sync
async def jobs_list(profile: str = _PROFILE, json_output: bool = JSON_OPTION) -> None:
    """Scheduled backup jobs."""
    try:
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            jobs = await client.get("/cluster/backup") or []
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    if json_output:
        emit_json(jobs)
        return
    if not jobs:
        print_info("No backup job")
        return
    table = Table(title=f"{len(jobs)} backup job(s)", header_style="bold cyan")
    for col in ("ID", "Enabled", "Schedule", "Guests", "Storage", "Mode", "Retention", "Next run", "Comment"):
        table.add_column(col, overflow="fold")
    for j in jobs:
        table.add_row(
            j.get("id", ""), "yes" if _enabled(j) else "no", str(j.get("schedule", "")), _selection(j),
            str(j.get("storage", "")), str(j.get("mode", "snapshot")),
            property_string(j["prune-backups"]) if j.get("prune-backups") else "storage",
            _when(j.get("next-run")), str(j.get("comment", "")),
        )
    console.print(table)


def _job_options(
    storage: str | None, mode: str | None, compress: str | None, notes_template: str | None,
    comment: str | None, node: str | None, keeps: dict[str, int | None],
) -> dict[str, Any]:
    if mode is not None and mode not in _MODES:
        raise PVECliError(f"--mode must be one of: {', '.join(_MODES)}")
    if compress is not None and compress not in _COMPRESS:
        raise PVECliError(f"--compress must be one of: {', '.join(_COMPRESS)}")
    data: dict[str, Any] = {}
    for key, value in (("storage", storage), ("mode", mode), ("compress", compress),
                       ("notes-template", notes_template), ("comment", comment), ("node", node)):
        if value is not None:
            data[key] = value
    spec = prune_spec(**keeps)
    if spec:
        data["prune-backups"] = spec
    return data


def _selection_args(vmid: str | None, everything: bool, pool: str | None, exclude: str | None) -> int:
    picked = sum(bool(x) for x in (vmid, everything, pool))
    if picked > 1:
        raise PVECliError("Pick one selection: --vmid, --all or --pool")
    if exclude and not everything:
        raise PVECliError("--exclude goes with --all")
    return picked


_K_LAST = typer.Option(None, "--keep-last", min=0, help="Retention: keep the N newest backups")
_K_DAILY = typer.Option(None, "--keep-daily", min=0, help="Retention: one per day, N days")
_K_WEEKLY = typer.Option(None, "--keep-weekly", min=0, help="Retention: one per week, N weeks")
_K_MONTHLY = typer.Option(None, "--keep-monthly", min=0, help="Retention: one per month, N months")
_K_YEARLY = typer.Option(None, "--keep-yearly", min=0, help="Retention: one per year, N years")


@jobs_app.command("add")
@async_to_sync
async def jobs_add(
    schedule: str = typer.Option(..., "--schedule", help="When: '02:00', 'sat 03:30', 'mon..fri 22:00'"),
    storage: str = typer.Option(..., "--storage", "-s", help="Backup storage"),
    vmid: str = typer.Option(None, "--vmid", help="Guests: 100,101"),
    everything: bool = typer.Option(False, "--all", help="Every guest"),
    pool: str = typer.Option(None, "--pool", help="Guests of this pool"),
    exclude: str = typer.Option(None, "--exclude", help="With --all: guests to leave out"),
    mode: str = typer.Option("snapshot", "--mode", help="snapshot, suspend or stop"),
    compress: str = typer.Option("zstd", "--compress", help="zstd, gzip, lzo or 0 (none; ignored by PBS)"),
    notes_template: str = typer.Option(None, "--notes-template", help="Notes of each backup ({{guestname}}...)"),
    comment: str = typer.Option(None, "--comment", help="Description of the job"),
    node: str = typer.Option(None, "--node", help="Only run on this node"),
    keep_last: int = _K_LAST, keep_daily: int = _K_DAILY, keep_weekly: int = _K_WEEKLY,
    keep_monthly: int = _K_MONTHLY, keep_yearly: int = _K_YEARLY,
    disabled: bool = typer.Option(False, "--disabled", help="Create the job disabled"),
    job_id: str = typer.Option(None, "--id", help="Job ID (default: generated by PVE)"),
    profile: str = _PROFILE_ONE,
) -> None:
    """Create a scheduled backup job.

    Without --keep-* the retention of the storage applies.

    Examples:
        pvecli backup jobs add --schedule 02:00 --all --exclude 9000 --storage pbs --keep-daily 7
        pvecli backup jobs add --schedule 'sat 03:00' --pool prod --storage nfs --mode stop
    """
    try:
        if not _selection_args(vmid, everything, pool, exclude):
            raise PVECliError("Pick the guests: --vmid, --all or --pool")
        data: dict[str, Any] = {"schedule": schedule, "enabled": 0 if disabled else 1}
        if vmid:
            data["vmid"] = vmid
        if everything:
            data["all"] = 1
        if pool:
            data["pool"] = pool
        if exclude:
            data["exclude"] = exclude
        if job_id:
            data["id"] = job_id
        data |= _job_options(storage, mode, compress, notes_template, comment, node,
                             {"last": keep_last, "daily": keep_daily, "weekly": keep_weekly,
                              "monthly": keep_monthly, "yearly": keep_yearly})
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            await client.post("/cluster/backup", data=data)
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    print_success(f"Backup job {job_id + ' ' if job_id else ''}created ({schedule}, {_selection(data)})")


@jobs_app.command("edit")
@async_to_sync
async def jobs_edit(
    job_id: str = typer.Argument(..., help="Job ID"),
    schedule: str = typer.Option(None, "--schedule", help="When: '02:00', 'sat 03:30'"),
    storage: str = typer.Option(None, "--storage", "-s", help="Backup storage"),
    vmid: str = typer.Option(None, "--vmid", help="Guests: 100,101 (replaces the selection)"),
    everything: bool = typer.Option(False, "--all", help="Every guest (replaces the selection)"),
    pool: str = typer.Option(None, "--pool", help="Guests of this pool (replaces the selection)"),
    exclude: str = typer.Option(None, "--exclude", help="With --all: guests to leave out"),
    mode: str = typer.Option(None, "--mode", help="snapshot, suspend or stop"),
    compress: str = typer.Option(None, "--compress", help="zstd, gzip, lzo or 0"),
    notes_template: str = typer.Option(None, "--notes-template", help="Notes of each backup"),
    comment: str = typer.Option(None, "--comment", help="Description of the job"),
    node: str = typer.Option(None, "--node", help="Only run on this node"),
    keep_last: int = _K_LAST, keep_daily: int = _K_DAILY, keep_weekly: int = _K_WEEKLY,
    keep_monthly: int = _K_MONTHLY, keep_yearly: int = _K_YEARLY,
    profile: str = _PROFILE_ONE,
) -> None:
    """Change a backup job: only the options given are sent."""
    try:
        _selection_args(vmid, everything, pool, exclude)
        data: dict[str, Any] = {}
        if schedule:
            data["schedule"] = schedule
        # A new selection replaces the old one, whichever kind it was
        if vmid:
            data |= {"vmid": vmid, "all": 0, "delete": "pool,exclude"}
        elif pool:
            data |= {"pool": pool, "all": 0, "delete": "vmid,exclude"}
        elif everything:
            data |= {"all": 1, "delete": "vmid,pool"}
            if exclude:
                data["exclude"] = exclude
        data |= _job_options(storage, mode, compress, notes_template, comment, node,
                             {"last": keep_last, "daily": keep_daily, "weekly": keep_weekly,
                              "monthly": keep_monthly, "yearly": keep_yearly})
        if not data:
            raise PVECliError("Nothing to change")
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            await _get_job(client, job_id)
            await client.put(f"/cluster/backup/{job_id}", data=data)
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    print_success(f"Backup job {job_id} updated")


async def _set_job(job_id: str, profile: str | None, data: dict[str, Any], what: str) -> None:
    try:
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            await _get_job(client, job_id)
            await client.put(f"/cluster/backup/{job_id}", data=data)
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    print_success(f"Backup job {job_id} {what}")


@jobs_app.command("enable")
@async_to_sync
async def jobs_enable(job_id: str = typer.Argument(..., help="Job ID"), profile: str = _PROFILE_ONE) -> None:
    """Enable a backup job."""
    await _set_job(job_id, profile, {"enabled": 1}, "enabled")


@jobs_app.command("disable")
@async_to_sync
async def jobs_disable(job_id: str = typer.Argument(..., help="Job ID"), profile: str = _PROFILE_ONE) -> None:
    """Disable a backup job (kept, not scheduled)."""
    await _set_job(job_id, profile, {"enabled": 0}, "disabled")


@jobs_app.command("remove")
@async_to_sync
async def jobs_remove(
    job_id: str = typer.Argument(..., help="Job ID"),
    yes: bool = _YES,
    profile: str = _PROFILE_ONE,
) -> None:
    """Delete a backup job (its backups are kept)."""
    try:
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            job = await _get_job(client, job_id)
            console.print(f"Backup job [cyan]{job_id}[/cyan]: {job.get('schedule', '')}, {_selection(job)}")
            if not yes and not confirm(f"Delete backup job {job_id}?", default=False):
                print_cancelled()
                return
            await client.delete(f"/cluster/backup/{job_id}")
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    print_success(f"Backup job {job_id} deleted")


@jobs_app.command("run")
@async_to_sync
async def jobs_run(
    job_id: str = typer.Argument(..., help="Job ID"),
    yes: bool = _YES,
    profile: str = _PROFILE_ONE,
) -> None:
    """Run a backup job now, with its own options, then check each guest.

    There is no 'run job' endpoint: this sends the job's options to vzdump
    on each node, with job-id when allowed (root@pam only).
    """
    try:
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            job = await _get_job(client, job_id)
            guests = {g["vmid"]: g for g in await _guests(client)}
            try:
                tree = await client.get(f"/cluster/backup/{job_id}/included_volumes") or {}
                ids = [int(c["id"]) for c in tree.get("children") or [] if str(c.get("id", "")).isdigit()]
            except PVECliError as e:
                if not is_not_implemented(e):
                    raise
                ids = _job_guests(job, list(guests.values()))
            missing = [v for v in ids if v not in guests]
            if missing:
                print_warning(f"Not in the cluster any more, skipped: {', '.join(map(str, missing))}")
            targets = [guests[v] for v in sorted(ids) if v in guests
                       and (not job.get("node") or guests[v].get("node") == job["node"])]
            if not targets:
                raise PVECliError(f"Backup job {job_id} selects no guest")
            options = {k: property_string(v) if isinstance(v, dict) else v
                       for k, v in job.items() if k not in _JOB_ONLY}
            params = {g["node"]: dict(options) for g in targets}
            console.print(
                f"Run backup job {job_id} now on {len(targets)} guest(s): "
                + ", ".join(f"{g['vmid']} ({g.get('name', '')})" for g in targets)
            )
            if not yes and not confirm(f"Back up these {len(targets)} guest(s)?", default=False):
                print_cancelled()
                return
            with console.status(f"Running backup job {job_id}..."):
                rows = await _vzdump(client, targets, params, job_id=job_id)
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    _report(rows)


def _job_guests(job: dict, guests: list[dict]) -> list[int]:
    """Guests of a job when PVE cannot list them (older versions)."""
    from ._shared import parse_id_list

    if job.get("vmid"):
        return parse_id_list(str(job["vmid"]), "guest")
    if job.get("pool"):
        return [g["vmid"] for g in guests if g.get("pool") == job["pool"]]
    excluded = set(parse_id_list(str(job["exclude"]), "guest")) if job.get("exclude") else set()
    return [g["vmid"] for g in guests if g["vmid"] not in excluded]
