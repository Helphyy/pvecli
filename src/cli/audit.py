"""Read-only health and hygiene audit of a cluster.

Nothing here changes anything. Each check reports OK, INFO, WARN or FAIL
with the detail needed to act. Exit code: 0 when nothing FAILs (warnings
included), 2 when something does, 1 when the audit itself could not run,
so a cron job can tell "act now" from "pvecli failed".
"""

import asyncio
import re
import time
from collections import Counter
from typing import Any

import typer
from rich.table import Table

from ..api.client import ProxmoxClient
from ..api.exceptions import PVECliError
from ..config import ConfigManager
from ..utils import JSON_OPTION, console, emit_json, print_error
from ..utils.fleet import fleet
from ..utils.helpers import async_to_sync
from .find import _parse_age

_LEVELS = {"OK": "green", "INFO": "cyan", "WARN": "yellow", "FAIL": "red"}
_DAY = 86400


class _Audit:
    def __init__(self, client: ProxmoxClient) -> None:
        self.client = client
        self.findings: list[dict[str, str]] = []
        self.denied: dict[str, str] = {}
        self.sem = asyncio.Semaphore(16)
        self._privileges: dict[str, dict | None] = {}

    def add(self, check: str, level: str, detail: str) -> None:
        self.findings.append({"check": check, "level": level, "detail": detail})

    def unreadable(self, check: str, why: str) -> None:
        """A source this user may not read: never reported as OK."""
        self.add(check, "INFO", f"not checked: {why}")

    async def get(self, endpoint: str, **params: Any) -> Any:
        """None when the call fails; the reason is kept in self.denied."""
        async with self.sem:
            try:
                return await self.client.get(endpoint, params=params or None)
            except PVECliError as e:
                self.denied[endpoint] = str(e)
                return None

    async def missing(self, path: str, *privileges: str) -> str | None:
        """None when the user holds one of these privileges on path, else what lacks.

        PVE drops from a list what the user may not see, without an error:
        a check built on such a list says so instead of reading it as complete.
        """
        if path not in self._privileges:
            data = await self.get("/access/permissions", path=path)
            self._privileges[path] = None if data is None else (data.get(path) or {})
        have = self._privileges[path]
        if have is not None and any(have.get(p) for p in privileges):
            return None
        return f"{' or '.join(privileges)} on {path}"

    def reason(self, path: str, default: str) -> str:
        text = self.denied.get(path, "")
        m = re.search(r"Permission check failed \(([^)]*)\)", text)
        return f"missing privilege {m.group(1)}" if m else (text or default)


async def _certificates(a: _Audit, nodes: list[str], warn_days: int) -> None:
    now = time.time()
    problems: list[tuple[str, str]] = []
    infos = await asyncio.gather(*(a.get(f"/nodes/{n}/certificates/info") for n in nodes))
    blind = [n for n, c in zip(nodes, infos, strict=True) if c is None]
    if blind:
        a.unreadable("Certificates", f"{', '.join(blind)} ({a.reason(f'/nodes/{blind[0]}/certificates/info', 'unreadable')})")
    for node, certs in zip(nodes, infos, strict=True):
        for c in certs or []:
            left = ((c.get("notafter") or 0) - now) / _DAY
            name = f"{node}:{c.get('filename')}"
            if left < 0:
                problems.append(("FAIL", f"{name} expired {int(-left)} day(s) ago"))
            elif left < 7:
                problems.append(("FAIL", f"{name} expires in {int(left)} day(s)"))
            elif left < warn_days:
                problems.append(("WARN", f"{name} expires in {int(left)} day(s)"))
    # OK is a claim about every node: not made when one could not be read
    if not problems and not blind:
        a.add("Certificates", "OK", f"no certificate expires within {warn_days} days")
    for level, detail in problems:
        a.add("Certificates", level, detail)


async def _users_and_tokens(a: _Audit) -> None:
    now = time.time()
    users = await a.get("/access/users", full=1)
    if users is None:
        a.unreadable("Users and tokens", a.reason("/access/users", "unreadable"))
        return
    found = False
    for u in users:
        uid = u.get("userid", "?")
        if u.get("enable", 1) and u.get("expire") and u["expire"] < now:
            a.add("Users", "INFO", f"{uid} has expired but is still enabled")
            found = True
        for t in u.get("tokens") or []:
            tid = f"{uid}!{t.get('tokenid')}"
            if t.get("expire") and t["expire"] < now:
                a.add("API tokens", "INFO", f"{tid} expired, remove it")
                found = True
            if not t.get("privsep", 1):
                level = "WARN" if uid == "root@pam" else "INFO"
                a.add(
                    "API tokens", level,
                    f"{tid} has privilege separation off: it holds every right of {uid}",
                )
                found = True
    hidden = await a.missing("/access/groups", "Sys.Audit", "User.Modify")
    if hidden:
        a.unreadable("Users and tokens", f"users hidden without {hidden}")
    elif not found:
        a.add("Users and tokens", "OK", f"{len(users)} user(s), nothing expired or over-privileged")


async def _acl(a: _Audit) -> None:
    acls = await a.get("/access/acl")
    if acls is None:
        a.unreadable("ACL", a.reason("/access/acl", "unreadable"))
        return
    broad = [
        x for x in acls
        if x.get("roleid") == "Administrator" and x.get("path") in ("/", "/vms", "/nodes", "/storage")
    ]
    for x in broad:
        a.add(
            "ACL", "WARN",
            f"{x.get('type')} {x.get('ugid')} is Administrator on {x.get('path')}"
            + (" (propagated)" if x.get("propagate", 1) else ""),
        )
    hidden = await a.missing("/access", "Sys.Audit")
    if hidden:
        a.unreadable("ACL", f"entries hidden without {hidden}")
    elif not broad:
        a.add("ACL", "OK", f"{len(acls)} ACL entries, no broad Administrator grant")


async def _firewall(a: _Audit) -> None:
    opts = await a.get("/cluster/firewall/options")
    if opts is None:
        a.add("Firewall", "INFO", "datacenter firewall options unreadable")
    elif not opts.get("enable"):
        a.add("Firewall", "WARN", "datacenter firewall is disabled: guest and node rules are not enforced")
    else:
        a.add("Firewall", "OK", f"enabled, input policy {opts.get('policy_in', 'DROP')}")
        # What Proxmox accepts without a word: a guest on with no card
        # filtered, rules left unenforced, rules broken by a missing object
        from .firewall import cluster_overview

        try:
            view = await cluster_overview(a.client)
        except PVECliError as e:
            a.unreadable("Firewall", f"the firewall of each level ({e})")
            return
        issues = [(view["datacenter"].scope.label, i) for i in view["datacenter"].issues]
        issues += [(st.scope.label, i) for st in view["levels"] for i in st.issues]
        for label, issue in issues:
            a.add("Firewall", "WARN", f"{label}: {issue}")


async def _versions_and_subscription(a: _Audit, nodes: list[str]) -> None:
    versions = await asyncio.gather(*(a.get(f"/nodes/{n}/version") for n in nodes))
    seen = {n: (v or {}).get("version", "?") for n, v in zip(nodes, versions, strict=True)}
    if "?" in seen.values():
        a.unreadable("PVE versions", "version of " + ", ".join(n for n, v in seen.items() if v == "?") + " unreadable")
    elif len(set(seen.values())) > 1:
        a.add("PVE versions", "WARN", "mixed: " + ", ".join(f"{n}={v}" for n, v in sorted(seen.items())))
    else:
        a.add("PVE versions", "OK", f"all on {next(iter(seen.values()), '?')}")
    subs = await asyncio.gather(*(a.get(f"/nodes/{n}/subscription") for n in nodes))
    states = Counter((s or {}).get("status", "unknown") for s in subs)
    if states.get("active") == len(nodes):
        a.add("Subscription", "OK", "active on every node")
    elif "expired" in states or "invalid" in states:
        a.add("Subscription", "WARN", ", ".join(f"{k}: {v}" for k, v in states.items()))
    else:
        a.add("Subscription", "INFO", ", ".join(f"{k}: {v} node(s)" for k, v in states.items()))
    updates = await asyncio.gather(*(a.get(f"/nodes/{n}/apt/update") for n in nodes))
    if any(u is None for u in updates):
        blind = [n for n, u in zip(nodes, updates, strict=True) if u is None]
        a.unreadable("Updates", f"{', '.join(blind)} ({a.reason(f'/nodes/{blind[0]}/apt/update', 'unreadable')})")
        return
    pending = {n: len(u or []) for n, u in zip(nodes, updates, strict=True)}
    if any(pending.values()):
        a.add(
            "Updates", "INFO",
            "pending (last apt refresh): " + ", ".join(f"{n}={c}" for n, c in sorted(pending.items()) if c),
        )
    else:
        a.add("Updates", "OK", "no pending package in the last apt refresh")


async def _guests(a: _Audit, guests: list[dict], snapshot_age: int) -> None:
    # Backups
    missing = await a.get("/cluster/backup-info/not-backed-up")
    if missing is None:
        a.unreadable("Backups", a.reason("/cluster/backup-info/not-backed-up", "backup coverage unreadable on this version"))
    elif missing:
        a.add(
            "Backups", "WARN",
            f"{len(missing)} guest(s) in no backup job: "
            + ", ".join(f"{m.get('vmid')} ({m.get('name', '')})" for m in missing[:10])
            + (" ..." if len(missing) > 10 else ""),
        )
    else:
        a.add("Backups", "OK", "every guest is covered by a backup job")

    # Snapshots and mounted ISOs, from each guest
    limit = time.time() - snapshot_age
    unread: list[int] = []

    async def inspect(g: dict) -> tuple[list[str], str | None]:
        base = f"/nodes/{g['node']}/{g['type']}/{g['vmid']}"
        snaps, cfg = await asyncio.gather(a.get(f"{base}/snapshot"), a.get(f"{base}/config"))
        if snaps is None:
            unread.append(g["vmid"])
        old = [s["name"] for s in snaps or [] if s.get("name") != "current" and (s.get("snaptime") or 0) < limit]
        iso = next(
            (str(v).split(",")[0] for k, v in (cfg or {}).items()
             if "media=cdrom" in str(v) and str(v).split(",")[0] not in ("none", "cdrom")),
            None,
        )
        return old, iso

    results = await asyncio.gather(*(inspect(g) for g in guests))
    old_snaps = [(g, o) for g, (o, _) in zip(guests, results, strict=True) if o]
    isos = [(g, i) for g, (_, i) in zip(guests, results, strict=True) if i]
    days = snapshot_age // _DAY
    label = f"{days}d" if snapshot_age % _DAY == 0 else f"{snapshot_age // 3600}h"
    if old_snaps:
        total = sum(len(o) for _, o in old_snaps)
        a.add(
            "Snapshots", "WARN",
            f"{total} snapshot(s) older than {label} on {len(old_snaps)} guest(s): "
            + ", ".join(f"{g['vmid']} ({len(o)})" for g, o in old_snaps[:10])
            + f". List them with: pvecli find --old-snapshots {label}",
        )
    hidden = await a.missing("/vms", "VM.Audit")
    if unread:
        a.unreadable("Snapshots", f"{len(unread)} guest(s) unreadable by this user")
    if hidden:
        a.unreadable("Snapshots", f"guests hidden without {hidden}, {len(guests)} visible")
    if not (old_snaps or unread or hidden):
        a.add("Snapshots", "OK", f"none older than {label}")
    if isos:
        a.add(
            "Mounted ISOs", "INFO",
            ", ".join(f"{g['vmid']}: {i}" for g, i in isos[:10]) + (" ..." if len(isos) > 10 else ""),
        )


async def _backup_freshness(a: _Audit, nodes: list[str], guests: list[dict], max_age: int) -> None:
    """Guests of an enabled backup job: last backup too old, or last run failed."""
    from .backup import collect_coverage

    check = "Backup freshness"
    try:
        cov = await collect_coverage(a.client, guests, nodes)
    except PVECliError as e:
        a.unreadable(check, str(e))
        return
    if cov["jobs"] is None:
        a.unreadable(check, f"backup jobs unreadable ({cov['jobs_error']})")
        return
    # Guests in no job at all are the "Backups" check's business
    covered = [g for g in guests if any(not j.endswith("(disabled)") for j in cov["jobs"].get(g["vmid"], []))]
    if not covered:
        a.add(check, "INFO", "no guest is in an enabled backup job")
        return
    days = max_age // _DAY
    label = f"{days}d" if max_age % _DAY == 0 else f"{max_age // 3600}h"
    failed, blind, old, unseen = [], [], [], []
    limit = time.time() - max_age
    for g in sorted(covered, key=lambda x: x["vmid"]):
        run = cov["runs"].get(g["vmid"])
        if g.get("node") in cov["blind"]:
            blind.append(g)
        elif run and run["state"] == "failed":
            failed.append(f"{g['vmid']} ({run['detail']})")
        last = cov["last"].get(g["vmid"])
        if not last and g["vmid"] in cov["hidden"]:
            # PVE drops its backups from the listing for this user: unknown
            unseen.append(g["vmid"])
        elif not last:
            old.append(f"{g['vmid']} (never)")
        elif last["ctime"] < limit:
            old.append(f"{g['vmid']} ({int((time.time() - last['ctime']) // _DAY)}d)")
    if failed:
        a.add(check, "FAIL", f"{len(failed)} guest(s) whose last backup run failed: " + ", ".join(failed[:10]))
    if cov["unreadable"]:
        # A newer backup may sit on the storage that could not be read
        a.unreadable(check, "backup age, storage(s) unreadable: "
                     + ", ".join(f"{k} ({v})" for k, v in list(cov["unreadable"].items())[:5]))
    elif old:
        a.add(check, "WARN", f"{len(old)} guest(s) in a backup job with no backup in the last {label}: "
              + ", ".join(old[:10]) + (" ..." if len(old) > 10 else ""))
    if blind:
        nodes_ = sorted({g["node"] for g in blind})
        a.unreadable(check, f"last run of {len(blind)} guest(s) on {', '.join(nodes_)} "
                     f"({cov['blind'][nodes_[0]]})")
    if unseen:
        a.unreadable(check, f"last backup of {len(unseen)} guest(s), hidden from this user: "
                     + ", ".join(map(str, unseen[:10])) + f" ({cov['hidden'][unseen[0]]})")
    if not (failed or old or blind or unseen or cov["unreadable"]):
        a.add(check, "OK", f"{len(covered)} guest(s) in a job, each backed up within {label}, last runs succeeded")


async def _orphan_disks(a: _Audit, nodes: list[str], guests: list[dict]) -> None:
    """Guest volumes left on a storage after their guest was removed."""
    # A volume is an orphan only against the full guest list, and only a
    # full storage list tells that no storage was left out
    scope = [m for m in [await a.missing("/vms", "VM.Audit"),
                         await a.missing("/storage", "Datastore.Audit", "Datastore.Allocate")] if m]
    if scope:
        a.unreadable("Orphan disks", f"guests or storages hidden without {', '.join(scope)}")
        return
    known = {g["vmid"] for g in guests}
    storages = [
        st for st in await a.get("/storage") or []
        if not st.get("disable") and any(c in str(st.get("content", "")) for c in ("images", "rootdir"))
    ]
    # PVE lists a guest volume only to who holds VM.Config.Disk on its guest
    # (an orphan's guest is gone: /vms decides) or Datastore.Allocate on the
    # storage, and silently drops the others: an empty list proves nothing
    disk = await a.missing("/vms", "VM.Config.Disk")
    allocate = await asyncio.gather(*(a.missing(f"/storage/{st['storage']}", "Datastore.Allocate") for st in storages))
    hidden: list[str] = []
    jobs = []
    for st, no_allocate in zip(storages, allocate, strict=True):
        if disk and no_allocate:
            hidden.append(st["storage"])
            continue
        # A storage limited to some nodes does not exist on the others,
        # which refuse to list it: never ask them
        allowed = {n.strip() for n in str(st.get("nodes") or "").split(",") if n.strip()}
        targets = [n for n in nodes if not allowed or n in allowed]
        # A shared storage answers the same from every node: ask once
        if st.get("shared"):
            targets = targets[:1]
        for n in targets:
            jobs.append((n, st["storage"]))
    contents = await asyncio.gather(*(a.get(f"/nodes/{n}/storage/{s}/content") for n, s in jobs))
    unread = [f"{s}@{n}" for (n, s), c in zip(jobs, contents, strict=True) if c is None]
    orphans: dict[str, str] = {}
    for (node, _), items in zip(jobs, contents, strict=True):
        for it in items or []:
            vmid = it.get("vmid")
            if it.get("content") in ("images", "rootdir") and vmid and int(vmid) not in known:
                orphans[it.get("volid", "?")] = node
    if orphans:
        a.add(
            "Orphan disks", "WARN",
            f"{len(orphans)} volume(s) belong to no existing guest: "
            + ", ".join(sorted(orphans)[:8]) + (" ..." if len(orphans) > 8 else ""),
        )
    if hidden:
        a.unreadable("Orphan disks", "volumes hidden without VM.Config.Disk on /vms or "
                     f"Datastore.Allocate on the storage: {', '.join(hidden[:5])}")
    if unread:
        a.unreadable("Orphan disks", f"storage(s) unreadable: {', '.join(unread[:5])}")
    if not (orphans or hidden or unread):
        a.add("Orphan disks", "OK", "every guest volume belongs to an existing guest")


async def _ha_and_ceph(a: _Audit) -> None:
    status = await a.get("/cluster/ha/status/current") or []
    armed = next((x.get("armed-state") for x in status if x.get("armed-state")), None)
    if armed == "disarmed":
        a.add("HA", "WARN", "the HA stack is disarmed: re-arm it with pvecli cluster ha arm")
    ceph = await a.get("/cluster/ceph/status")
    if ceph:
        health = (ceph.get("health") or {}).get("status", "?")
        level = {"HEALTH_OK": "OK", "HEALTH_WARN": "WARN"}.get(health, "FAIL")
        checks = ", ".join((ceph.get("health") or {}).get("checks", {}) or [])
        a.add("Ceph", level, health + (f": {checks}" if checks else ""))
        flags = await a.get("/cluster/ceph/flags") or []
        left = [f.get("name") for f in flags if f.get("value")]
        if left:
            a.add("Ceph flags", "WARN", "still set: " + ", ".join(left) + " (forgotten after maintenance?)")


@fleet(parallel=True)
@async_to_sync
async def audit(
    cert_days: int = typer.Option(30, "--cert-days", help="Warn on certificates expiring within this many days"),
    snapshot_age: str = typer.Option("30d", "--snapshot-age", help="Warn on snapshots older than this (30d, 2w)"),
    backup_age: str = typer.Option("2d", "--backup-age", help="Warn on guests of a backup job whose last backup is older (2d, 36h)"),
    skip_orphans: bool = typer.Option(False, "--skip-orphans", help="Skip the orphan disk scan (lists every storage)"),
    only_problems: bool = typer.Option(False, "--problems", help="Hide OK lines"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile(s): name, list, pattern ('client-*') or @group"),
    json_output: bool = JSON_OPTION,
) -> None:
    """Audit a cluster: certificates, access, firewall, versions, backups, snapshots, orphans.

    Read only. Exit code 0 when nothing fails (warnings included), 2 when a
    check fails, 1 when the audit could not run.

    Examples:
        pvecli audit
        pvecli audit -p '*' --problems
        pvecli audit -p @prod --json
    """
    age = _parse_age(snapshot_age)
    max_backup_age = _parse_age(backup_age)
    try:
        async with ProxmoxClient(ConfigManager().get_profile(profile)) as client:
            a = _Audit(client)
            nodes = sorted(
                n.get("node", "") for n in await client.get_nodes() if n.get("status") == "online"
            )
            guests = [
                g for g in await client.get_cluster_resources(resource_type="vm")
                if g.get("type") in ("qemu", "lxc")
            ]
            steps = [
                _certificates(a, nodes, cert_days),
                _users_and_tokens(a),
                _acl(a),
                _firewall(a),
                _versions_and_subscription(a, nodes),
                _guests(a, guests, age),
                _backup_freshness(a, nodes, guests, max_backup_age),
                _ha_and_ceph(a),
            ]
            if not skip_orphans:
                steps.append(_orphan_disks(a, nodes, guests))
            await asyncio.gather(*steps)
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)

    order = ["FAIL", "WARN", "INFO", "OK"]
    findings = sorted(a.findings, key=lambda f: (order.index(f["level"]), f["check"]))
    if only_problems:
        findings = [f for f in findings if f["level"] in ("FAIL", "WARN")]
    counts = Counter(f["level"] for f in a.findings)

    if json_output:
        emit_json({"findings": findings, "counts": dict(counts)})
    else:
        table = Table(header_style="bold cyan")
        table.add_column("Check", style="cyan")
        table.add_column("Level")
        table.add_column("Detail", overflow="fold")
        for f in findings:
            color = _LEVELS[f["level"]]
            table.add_row(f["check"], f"[{color}]{f['level']}[/{color}]", f["detail"])
        if findings:
            console.print(table)
        console.print(
            "  " + "  ".join(f"[{_LEVELS[k]}]{k} {counts.get(k, 0)}[/{_LEVELS[k]}]" for k in order)
        )
    if counts.get("FAIL"):
        raise typer.Exit(2)
