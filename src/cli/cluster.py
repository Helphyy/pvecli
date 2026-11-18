"""Cluster management commands."""

import asyncio
import os
import time
from getpass import getpass

import typer
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Prompt
from rich.table import Table

from ..api.client import ProxmoxClient, is_not_implemented
from ..api.exceptions import PVECliError
from ..config import ConfigManager
from ..utils import (
    JSON_OPTION,
    confirm,
    console,
    emit_json,
    format_bytes,
    format_percentage,
    format_uptime,
    get_status_color,
    menu_prompt,
    parse_tags,
    print_cancelled,
    print_error,
    print_info,
    print_success,
    print_warning,
)
from ..utils.fleet import fleet
from ..utils.helpers import async_to_sync
from ._shared import ceph_absent, detect_connected_node, shared_usage

app = typer.Typer(help="Manage cluster", no_args_is_help=True)


# ── cluster preflight ────────────────────────────────────────────────────

# What a check can conclude. Order matters: the worst one wins for the exit code.
_OK, _WARN, _FAIL, _SKIP = "ok", "warn", "fail", "skip"
_MARK = {
    _OK: "[green]OK[/green]",
    _WARN: "[yellow]WARN[/yellow]",
    _FAIL: "[red]FAIL[/red]",
    _SKIP: "[dim]n/a[/dim]",
}


# /nodes/{node}/time returns whole seconds, so two perfectly synchronised
# clocks can still read one second apart. Drift is only reported beyond that.
_CLOCK_RESOLUTION = 1.0


async def _clock_offset(client: ProxmoxClient, node: str) -> float | None:
    """Node clock minus local clock, in seconds, or None if unreadable.

    The local clock is read around this single call, so API latency, other
    requests and time spent at a prompt never show up as drift. Comparing two
    offsets compares the two node clocks, however far apart they were read.
    """
    before = time.time()
    try:
        data = await client.get_node_time(node) or {}
    except PVECliError:
        return None
    after = time.time()
    node_time = data.get("time")
    if not isinstance(node_time, int):
        return None
    return node_time - (before + after) / 2


def _drift_seconds(offsets: list[float]) -> float:
    """Clock drift between offsets, minus what the 1 s API resolution explains."""
    if len(offsets) < 2:
        return 0.0
    return max(0.0, max(offsets) - min(offsets) - _CLOCK_RESOLUTION)


def _hosts_names(raw_hosts: str) -> set[str]:
    """Host names declared in an /etc/hosts file, both full and short forms."""
    names: set[str] = set()
    for line in raw_hosts.splitlines():
        fields = line.split("#", 1)[0].split()
        for name in fields[1:]:
            names.add(name)
            names.add(name.split(".", 1)[0])
    return names


async def _node_facts(client: ProxmoxClient, node: str) -> dict:
    """Everything the checks need about one node. Never raises."""
    entry: dict = {"clock_offset": await _clock_offset(client, node)}
    for key, call in (
        ("version", client.get_node_version),
        ("dns", client.get_node_dns),
        ("hosts", client.get_node_hosts),
        ("services", client.get_node_services),
    ):
        try:
            entry[key] = await call(node) or {}
        except PVECliError as e:
            entry[key] = {"_error": str(e)}
    return entry


async def _gather_node_facts(client: ProxmoxClient, nodes: list[str]) -> dict[str, dict]:
    """Collect the facts of every node concurrently. Never raises."""
    results = await asyncio.gather(*(_node_facts(client, n) for n in nodes))
    return dict(zip(nodes, results, strict=True))


@app.command("preflight")
@fleet(parallel=True)
@async_to_sync
async def cluster_preflight(
    ntp_drift: int = typer.Option(2, "--max-drift", help="Clock drift tolerated between nodes, in seconds"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    json_output: bool = JSON_OPTION,
) -> None:
    """Check what silently breaks a cluster join or a maintenance window."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            nodes_raw = await client.get_nodes()
            online = [n.get("node", "") for n in nodes_raw if n.get("status") == "online"]
            offline = [n.get("node", "") for n in nodes_raw if n.get("status") != "online"]

            if not online:
                print_error("No online node found")
                raise typer.Exit(1)

            # Is this a cluster at all, or a standalone node about to join one
            try:
                # A standalone node answers data:null here rather than erroring
                totem = await client.get_cluster_totem() or {}
            except PVECliError:
                totem = {}
            clustered = bool(totem.get("cluster_name"))

            facts = await _gather_node_facts(client, online)
            checks: list[tuple[str, str, str]] = []  # (check, verdict, detail)

            # 1. Node reachability
            if offline:
                checks.append(("Nodes online", _FAIL, f"{len(online)} online, offline: {', '.join(offline)}"))
            else:
                checks.append(("Nodes online", _OK, f"{len(online)}/{len(nodes_raw)}"))

            # 2. PVE versions. Proxmox refuses a join across releases (9.1 vs
            # 9.2), so those fail. A patch level gap (9.1.1 vs 9.1.2) joins
            # fine but is still worth flagging before a maintenance window.
            versions = {
                n: (f.get("version") or {}).get("version", "?") for n, f in facts.items()
            }
            releases = {
                n: (f.get("version") or {}).get("release", "?") for n, f in facts.items()
            }
            detail = ", ".join(f"{n}={v}" for n, v in sorted(versions.items()))
            distinct = sorted(set(versions.values()))
            if len(set(releases.values())) > 1:
                checks.append(("PVE versions", _FAIL, detail))
            elif len(distinct) > 1:
                checks.append(("PVE versions", _WARN, f"same release, different patch level: {detail}"))
            else:
                checks.append(("PVE versions", _OK, f"all on {distinct[0] if distinct else '?'}"))

            # 3. Clock drift: corosync and the join both depend on it
            offsets = [
                f["clock_offset"] for f in facts.values() if f.get("clock_offset") is not None
            ]
            if len(offsets) < 2:
                checks.append(("Clock drift", _SKIP, "single node"))
            else:
                drift = _drift_seconds(offsets)
                verdict = _OK if drift <= ntp_drift else (_WARN if drift <= 30 else _FAIL)
                shown = "under 1s" if drift < 1 else f"{drift:.0f}s"
                checks.append(("Clock drift", verdict, f"{shown} between nodes (max {ntp_drift}s)"))

            # 4. Name resolution: every node must resolve every other one, either
            # through /etc/hosts or through DNS. Missing entries are the most
            # common cause of a join that hangs. Only /etc/hosts can be checked
            # through the API: a configured DNS server says nothing about what
            # it answers, so those pairs are reported as unverified, not OK.
            unresolved: list[str] = []
            dns_only: list[str] = []
            for node, f in facts.items():
                declared = _hosts_names((f.get("hosts") or {}).get("data", "") or "")
                has_dns = bool((f.get("dns") or {}).get("dns1"))
                for other in online:
                    if other == node or other in declared:
                        continue
                    (dns_only if has_dns else unresolved).append(f"{node}->{other}")
            if len(online) < 2:
                checks.append(("Name resolution", _SKIP, "single node"))
            elif unresolved:
                checks.append((
                    "Name resolution",
                    _WARN,
                    f"{len(unresolved)} pair(s) rely on neither hosts nor DNS: "
                    + ", ".join(unresolved[:4]),
                ))
            elif dns_only:
                checks.append((
                    "Name resolution",
                    _SKIP,
                    f"not verified: {len(dns_only)} pair(s) rely on DNS, missing from "
                    f"/etc/hosts ({', '.join(dns_only[:3])}). Check with: getent hosts <node>",
                ))
            else:
                checks.append(("Name resolution", _OK, "every node lists the others in /etc/hosts"))

            # 5. Core services
            wanted = {"pve-cluster", "pvedaemon", "pveproxy", "pvestatd"}
            broken: list[str] = []
            for node, f in facts.items():
                svcs = f.get("services")
                if not isinstance(svcs, list):
                    broken.append(f"{node} (unreadable)")
                    continue
                for s in svcs:
                    if s.get("name") in wanted and s.get("state") != "running":
                        broken.append(f"{node}:{s.get('name')}={s.get('state')}")
            checks.append(
                ("Core services", _FAIL if broken else _OK,
                 ", ".join(broken) if broken else "pve-cluster, pvedaemon, pveproxy, pvestatd running")
            )

            # 6. Cluster specific checks
            if clustered:
                status = await client.get_cluster_status()
                cluster_entry = next((x for x in status if x.get("type") == "cluster"), {})
                quorate = cluster_entry.get("quorate")
                checks.append((
                    "Quorum",
                    _OK if quorate else _FAIL,
                    "quorate" if quorate else "NOT quorate, /etc/pve is read-only",
                ))

                links_readable = True
                try:
                    join_info = await client.get_cluster_join_info() or {}
                    nodelist = join_info.get("nodelist", []) or []
                    link_keys = {
                        k for entry in nodelist for k in entry if k.startswith("ring") and k.endswith("_addr")
                    }
                except PVECliError:
                    link_keys = set()
                    links_readable = False
                if not links_readable:
                    checks.append((
                        "Corosync links",
                        _WARN,
                        "could not read the corosync nodelist to count links",
                    ))
                elif len(link_keys) <= 1:
                    checks.append((
                        "Corosync links",
                        _WARN,
                        "single link: corosync has no redundancy, a network outage splits the cluster",
                    ))
                else:
                    checks.append(("Corosync links", _OK, f"{len(link_keys)} links"))

                try:
                    qdev = await client.get_cluster_qdevice() or {}
                except PVECliError:
                    qdev = {}
                has_qdevice = bool(qdev)
                if len(online) == 2 and not has_qdevice:
                    checks.append((
                        "Quorum resilience",
                        _WARN,
                        "2 nodes without QDevice: losing either one blocks the cluster",
                    ))
                elif has_qdevice:
                    checks.append(("Quorum resilience", _OK, "QDevice present"))
                else:
                    checks.append((
                        "Quorum resilience",
                        _OK,
                        f"{len(online)} nodes, tolerates {(len(online) - 1) // 2} loss(es)",
                    ))
            else:
                checks.append(("Cluster", _SKIP, "standalone node, ready to create or join"))
                try:
                    resources = await client.get_cluster_resources(resource_type="vm") or []
                except PVECliError:
                    resources = []
                if resources:
                    checks.append((
                        "Guests present",
                        _WARN,
                        f"{len(resources)} guest(s): joining a cluster wipes this node's /etc/pve",
                    ))
                else:
                    checks.append(("Guests present", _OK, "none, safe to join"))

            worst = _FAIL if any(v == _FAIL for _, v, _ in checks) else (
                _WARN if any(v == _WARN for _, v, _ in checks) else _OK
            )

            if json_output:
                emit_json({
                    "cluster": totem.get("cluster_name") or None,
                    "nodes": online,
                    "verdict": worst,
                    "checks": [
                        {"check": c, "verdict": v, "detail": d} for c, v, d in checks
                    ],
                })
                # Same exit code as the text output, so scripts can rely on it
                if worst == _FAIL:
                    raise typer.Exit(1)
                return

            title = (
                f"Preflight: cluster '{totem.get('cluster_name')}'"
                if clustered
                else f"Preflight: standalone node ({online[0]})"
            )
            table = Table(title=title, show_header=True, header_style="bold cyan")
            table.add_column("Check", style="cyan")
            table.add_column("Result")
            table.add_column("Detail")
            for check, verdict, detail in checks:
                table.add_row(check, _MARK[verdict], detail)
            console.print(table)

            if worst == _FAIL:
                print_error("Blocking issues found, fix them before joining or before maintenance")
                raise typer.Exit(1)
            if worst == _WARN:
                print_warning("No blocker, but the points above deserve a look")
            else:
                print_success("All checks passed")

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("health")
@fleet(parallel=True)
@async_to_sync
async def cluster_health(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    json_output: bool = JSON_OPTION,
) -> None:
    """Quorum, node votes, HA and Ceph state in one view."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            status = await client.get_cluster_status() or []
            cluster_entry = next((x for x in status if x.get("type") == "cluster"), {})
            node_entries = [x for x in status if x.get("type") == "node"]

            if not cluster_entry:
                if json_output:
                    emit_json({"cluster": None, "standalone": True})
                    return
                print_info("Standalone node: no quorum to report")
                return

            name = cluster_entry.get("name", "?")
            quorate = bool(cluster_entry.get("quorate"))
            online = [n for n in node_entries if n.get("online")]
            offline = [n for n in node_entries if not n.get("online")]

            # Votes come from the corosync nodelist, not from /cluster/status
            try:
                join_info = await client.get_cluster_join_info() or {}
            except PVECliError:
                join_info = {}
            nodelist = join_info.get("nodelist") or []
            votes: dict[str, int] = {}
            votes_known = bool(nodelist)
            for entry in nodelist:
                raw = entry.get("quorum_votes")
                if raw is None:
                    votes_known = False
                    continue
                try:
                    votes[entry.get("name", "")] = int(raw)
                except (TypeError, ValueError):
                    votes_known = False

            try:
                qdevice = await client.get_cluster_qdevice() or {}
            except PVECliError:
                qdevice = {}
            has_qdevice = bool(qdevice)

            # Tolerance is only meaningful when every vote is known and no
            # arbiter is in play: a qdevice adds votes that no endpoint
            # exposes, and a wrong number here would be used to decide
            # whether a node can be shut down.
            total_votes = sum(votes.values()) if votes_known else 0
            online_votes = (
                sum(votes.get(n.get("name", ""), 0) for n in online) if votes_known else 0
            )
            if not votes_known or has_qdevice:
                tolerance: int | None = None
                tolerance_text = (
                    "not computable: a QDevice contributes votes that the API does not expose"
                    if has_qdevice
                    else "not computable: some node votes are unknown"
                )
            else:
                needed = total_votes // 2 + 1
                tolerance = max(0, online_votes - needed)
                tolerance_text = (
                    f"{online_votes}/{total_votes} votes online, quorum needs {needed}, "
                    f"can lose {tolerance} more node(s)"
                )

            # HA and Ceph, both optional
            try:
                ha_raw = await client.get("/cluster/ha/status/current") or []
            except PVECliError:
                ha_raw = []
            # Only 'service' entries are guests: the list also holds the
            # quorum, the CRM master and one LRM per node.
            ha_resources = [x for x in ha_raw if x.get("type") == "service"]
            # PVE 9.2+ can disarm the whole HA stack; older versions have no
            # such state and never report the field.
            ha_armed = next(
                (x.get("armed-state") for x in ha_raw if x.get("armed-state")), None
            )

            ceph_state = None
            try:
                ceph = await client.get("/cluster/ceph/status") or {}
                ceph_state = (ceph.get("health") or {}).get("status")
            except PVECliError:
                ceph_state = None

            if json_output:
                emit_json({
                    "cluster": name,
                    "quorate": quorate,
                    "nodes": {
                        "online": [n.get("name") for n in online],
                        "offline": [n.get("name") for n in offline],
                    },
                    "votes": votes if votes_known else None,
                    "qdevice": has_qdevice,
                    "tolerance": tolerance,
                    "ha_resources": len(ha_resources),
                    "ha_armed_state": ha_armed,
                    "ceph": ceph_state,
                })
                # Same exit code as the text output, so scripts can rely on it
                if not quorate:
                    raise typer.Exit(1)
                return

            table = Table(title=f"Health of cluster '{name}'", show_header=True, header_style="bold cyan")
            table.add_column("Item", style="cyan")
            table.add_column("State")
            table.add_column("Detail")

            table.add_row(
                "Quorum",
                "[green]quorate[/green]" if quorate else "[red]lost[/red]",
                "cluster is writable" if quorate else "/etc/pve is read-only cluster wide",
            )
            table.add_row(
                "Nodes",
                f"[green]{len(online)}/{len(node_entries)}[/green]"
                if not offline
                else f"[red]{len(online)}/{len(node_entries)}[/red]",
                "all online" if not offline else "offline: " + ", ".join(
                    str(n.get("name")) for n in offline
                ),
            )
            table.add_row(
                "Failure tolerance",
                "[green]ok[/green]" if tolerance else ("[dim]unknown[/dim]" if tolerance is None else "[red]none[/red]"),
                tolerance_text,
            )
            table.add_row(
                "QDevice",
                "[green]present[/green]" if has_qdevice else "[dim]none[/dim]",
                "arbiter configured" if has_qdevice else "no arbiter",
            )
            ha_detail = (
                f"{len(ha_resources)} resource(s) under HA" if ha_resources else "no HA resource"
            )
            if ha_armed:
                # standby: armed, but no watchdog active since nothing is under HA
                style = {"armed": "green", "standby": "dim"}.get(ha_armed, "yellow")
                ha_detail += (
                    ". Re-arm with: pvecli cluster ha arm" if ha_armed == "disarmed" else ""
                )
                table.add_row("HA", f"[{style}]{ha_armed}[/{style}]", ha_detail)
            else:
                table.add_row(
                    "HA resources",
                    "[green]active[/green]" if ha_resources else "[dim]none[/dim]",
                    ha_detail,
                )
            table.add_row(
                "Ceph",
                {
                    "HEALTH_OK": "[green]HEALTH_OK[/green]",
                    "HEALTH_WARN": "[yellow]HEALTH_WARN[/yellow]",
                    "HEALTH_ERR": "[red]HEALTH_ERR[/red]",
                }.get(ceph_state or "", "[dim]absent[/dim]"),
                ceph_state or "no Ceph on this cluster",
            )
            console.print(table)

            if not quorate:
                print_error("Cluster is not quorate: fix that before any maintenance")
                raise typer.Exit(1)
            if tolerance == 0:
                print_warning(
                    "No margin left: losing one more node makes the cluster read-only"
                )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("create")
@async_to_sync
async def cluster_create(
    name: str = typer.Argument(None, help="Cluster name"),
    link0: str = typer.Option(None, "--link0", help="Address of this node on corosync link 0"),
    link1: str = typer.Option(None, "--link1", help="Address on corosync link 1"),
    yes: bool = typer.Option(False, "--yes", "-y", is_flag=True, help="Skip the typed confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile of the standalone node"),
) -> None:
    """Turn the node of this profile into the first member of a new cluster."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            try:
                totem = await client.get_cluster_totem() or {}
            except PVECliError:
                totem = {}
            if totem.get("cluster_name"):
                print_error(
                    f"This node already belongs to cluster '{totem['cluster_name']}'"
                )
                raise typer.Exit(1)

            nodes = await client.get_nodes() or []
            node_name = nodes[0].get("node", "?") if nodes else "?"
            guests = await client.get_cluster_resources(resource_type="vm") or []

            if name is None:
                name = menu_prompt("  Cluster name")
            if not name:
                print_cancelled()
                return

            console.print("\n[bold cyan]═══ Create cluster ═══[/bold cyan]\n")
            console.print(f"  [bold]Node:[/bold]         {node_name} ({profile_config.host})")
            console.print(f"  [bold]Cluster name:[/bold] {name}")
            if link0:
                console.print(f"  [bold]link0:[/bold]        {link0}")
            if link1:
                console.print(f"  [bold]link1:[/bold]        {link1}")

            if guests:
                print_warning(f"{len(guests)} guest(s) already live on this node")
            print_warning(
                "A cluster name cannot be changed, and a cluster cannot be "
                "dissolved through the API. Getting it wrong means reinstalling the node."
            )

            if not yes:
                typed = menu_prompt(f"  Type the cluster name '{name}' again to confirm")
                if typed != name:
                    print_error("Name does not match")
                    print_cancelled()
                    raise typer.Exit(1)

            links = {f"link{n}": v for n, v in (("0", link0), ("1", link1)) if v}
            console.print(f"\n[cyan]Creating cluster '{name}'...[/cyan]")
            await client.create_cluster(name, **links)

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
        return
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)

    # Corosync and pveproxy restart during creation, so verify on a fresh
    # connection rather than trusting the call that just returned.
    console.print("[cyan]Waiting for the cluster to become quorate...[/cyan]")
    for _ in range(24):
        await asyncio.sleep(5)
        try:
            async with ProxmoxClient(profile_config) as verify:
                status = await verify.get_cluster_status() or []
                entry = next((x for x in status if x.get("type") == "cluster"), {})
                if entry.get("name") == name and entry.get("quorate"):
                    print_success(f"Cluster '{name}' created and quorate")
                    print_info(
                        "Add nodes with: pvecli cluster join <node address> "
                        f"-p {profile or config_manager.get().default_profile}"
                    )
                    return
        except PVECliError:
            continue

    print_error(
        f"Cluster '{name}' did not report itself quorate within the timeout. "
        "Check 'pvecli cluster status'."
    )
    raise typer.Exit(1)


@app.command("join")
@async_to_sync
async def cluster_join(
    target: str = typer.Argument(None, help="Address of the standalone node that will join"),
    target_user: str = typer.Option("root@pam", "--target-user", help="API user on the joining node"),
    target_password_env: str = typer.Option(None, "--target-password-env", help="Env var holding the joining node's password"),
    peer_password_env: str = typer.Option(None, "--peer-password-env", help="Env var holding the cluster's root password"),
    link0: str = typer.Option(None, "--link0", help="Address of the joining node on corosync link 0"),
    link1: str = typer.Option(None, "--link1", help="Address on corosync link 1"),
    yes: bool = typer.Option(False, "--yes", "-y", is_flag=True, help="Skip the typed confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile of the cluster to join"),
) -> None:
    """Make a standalone node join the cluster of the current profile."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        # ── The cluster side: who are we joining, and with which fingerprint ──
        async with ProxmoxClient(profile_config) as client:
            # A standalone node raises "node is not in a cluster" here. Any
            # other error is transient (a join in progress restarts corosync
            # and pveproxy on the peer) and deserves a retry, not a wrong
            # "this is not a cluster" verdict.
            join_info: dict = {}
            last_error: PVECliError | None = None
            for _attempt in range(6):
                try:
                    join_info = await client.get_cluster_join_info() or {}
                    last_error = None
                    break
                except PVECliError as e:
                    last_error = e
                    if "not in a cluster" in str(e):
                        break
                    await asyncio.sleep(5)
            if last_error is not None and "not in a cluster" not in str(last_error):
                print_error(f"Could not read the cluster join information: {last_error}")
                raise typer.Exit(1)
            nodelist = join_info.get("nodelist") or []
            totem = (join_info.get("totem") or {}) or (await client.get_cluster_totem() or {})
            cluster_name = totem.get("cluster_name")

            if not cluster_name or not nodelist:
                print_error(
                    f"Profile '{profile or 'default'}' does not point at a cluster. "
                    "Join needs an existing cluster as the target."
                )
                raise typer.Exit(1)

            # The fingerprint must belong to the node we hand to the joining
            # node as 'hostname'. Falling back to nodelist[0] would pair the
            # address of one node with the certificate of another.
            peer_host = profile_config.host
            peer_entry = next(
                (
                    n
                    for n in nodelist
                    if n.get("pve_addr") == peer_host or n.get("name") == peer_host
                ),
                None,
            )
            if peer_entry is None:
                peer_entry = nodelist[0]
                peer_host = peer_entry.get("pve_addr") or peer_entry.get("name") or peer_host
                print_info(
                    f"Profile host does not match a cluster member, using "
                    f"{peer_entry.get('name')} ({peer_host}) as the peer"
                )
            fingerprint = peer_entry.get("pve_fp")
            if not fingerprint:
                print_error("Could not read the cluster fingerprint")
                raise typer.Exit(1)

            # Facts about the cluster side, needed once the client is closed
            peer_node = peer_entry.get("name") or nodelist[0].get("name")
            peer_offset = await _clock_offset(client, peer_node)
            try:
                peer_ver = await client.get_node_version(peer_node) or {}
                peer_version, peer_release = peer_ver.get("version"), peer_ver.get("release")
            except PVECliError:
                peer_version = peer_release = None

            # How many corosync links does this cluster use? Joining a
            # multi-link cluster with a single link is silently accepted by
            # the API and leaves corosync without redundancy.
            link_numbers = sorted(
                {
                    k[len("ring"):-len("_addr")]
                    for n in nodelist
                    for k in n
                    if k.startswith("ring") and k.endswith("_addr")
                }
            )

        if target is None:
            target = menu_prompt("  Address of the node that will join")
        if not target:
            print_cancelled()
            return

        # ── Credentials, never on the command line ──
        target_password = os.environ.get(target_password_env or "", "")
        if not target_password:
            target_password = getpass(f"  root password of the joining node {target}: ")
        if not target_password:
            print_error("The joining node's password is required")
            raise typer.Exit(1)

        peer_password = os.environ.get(peer_password_env or "", "")
        if not peer_password and profile_config.auth.type == "password":
            # The API wants the peer's root@pam password specifically, so the
            # profile password only fits when the profile is root@pam itself.
            if profile_config.auth.user == "root@pam":
                peer_password = profile_config.auth.password or ""
                print_info("Using the root@pam password from the profile for the peer")
            else:
                print_info(
                    f"Profile authenticates as {profile_config.auth.user}, "
                    "but the join needs the peer's root@pam password"
                )
        if not peer_password:
            peer_password = getpass(f"  root password of the cluster '{cluster_name}': ")
        if not peer_password:
            print_error("The cluster's root password is required")
            raise typer.Exit(1)

        # ── Preflight on the joining node: joining wipes its /etc/pve ──
        target_client = ProxmoxClient.for_host(
            target,
            target_user,
            target_password,
            port=profile_config.port,
            verify_ssl=profile_config.verify_ssl,
        )
        async with target_client as tc:
            t_totem = await tc.get_cluster_totem() or {}
            if t_totem.get("cluster_name"):
                print_error(
                    f"'{target}' already belongs to cluster '{t_totem['cluster_name']}'. "
                    "A node can only be in one cluster."
                )
                raise typer.Exit(1)

            # A join between different PVE releases is refused by Proxmox, and
            # corosync needs synchronized clocks. Both are measured here rather
            # than reminded about, so the message only fires when it applies.
            blocking: list[str] = []

            try:
                t_ver = await tc.get_node_version(
                    (await tc.get_nodes() or [{}])[0].get("node", "")
                ) or {}
                t_version, t_release = t_ver.get("version"), t_ver.get("release")
            except PVECliError:
                t_version = t_release = None
            if peer_release and t_release and peer_release != t_release:
                blocking.append(
                    f"PVE releases differ: cluster runs {peer_version}, "
                    f"'{target}' runs {t_version}. Proxmox refuses such a join."
                )
            elif peer_version and t_version and peer_version != t_version:
                # Same release, different patch level: Proxmox accepts it, but a
                # cluster is easier to reason about when every node matches.
                print_warning(
                    f"PVE patch levels differ: cluster runs {peer_version}, "
                    f"'{target}' runs {t_version}. The join is allowed, but "
                    "upgrading both sides first is recommended."
                )

            # Offsets, not raw epochs: the two clocks are read minutes apart
            # (password prompt in between), which must not count as drift.
            t_offset = await _clock_offset(tc, (await tc.get_nodes() or [{}])[0].get("node", ""))
            drift = (
                round(_drift_seconds([peer_offset, t_offset]))
                if peer_offset is not None and t_offset is not None
                else None
            )

            t_guests = await tc.get_cluster_resources(resource_type="vm") or []
            t_nodes = await tc.get_nodes() or []
            if not t_nodes or not t_nodes[0].get("node"):
                print_error(f"Could not read the node name of '{target}'")
                raise typer.Exit(1)
            t_name = t_nodes[0]["node"]

            console.print("\n[bold cyan]═══ Cluster join ═══[/bold cyan]\n")
            console.print(f"  [bold]Joining node:[/bold]  {t_name} ({target})")
            console.print(f"  [bold]Target cluster:[/bold] {cluster_name}")
            console.print(
                "  [bold]Existing members:[/bold] "
                + ", ".join(n.get("name", "?") for n in nodelist)
            )
            console.print(f"  [bold]Peer:[/bold]           {peer_host}")
            console.print(f"  [bold]Fingerprint:[/bold]    {fingerprint}")

            if len(link_numbers) > 1:
                provided = {"0": link0, "1": link1}
                missing = [n for n in link_numbers if not provided.get(n)]
                if missing:
                    # Only --link0 and --link1 exist, so a cluster on ring2 and
                    # above cannot be joined from here. Say that instead of
                    # naming a flag Typer would reject.
                    unsupported = [n for n in missing if n not in provided]
                    print_error(
                        f"The cluster uses {len(link_numbers)} corosync links "
                        f"(ring{', ring'.join(link_numbers)}). Joining without "
                        f"ring{', ring'.join(missing)} would leave this node "
                        "without corosync redundancy."
                    )
                    if unsupported:
                        print_info(
                            "pvecli only carries --link0 and --link1: join this "
                            "cluster from the web UI or with pvecm add."
                        )
                    else:
                        print_info(
                            "Pass " + " ".join(f"--link{n} <address>" for n in missing)
                        )
                    raise typer.Exit(1)

            if drift is not None and drift > 30:
                blocking.append(
                    f"Clocks differ by {drift}s between the cluster and '{target}'. "
                    "Corosync will not form a stable membership. Synchronize the "
                    "clocks first, for instance with: timedatectl set-ntp true"
                )
            elif drift is not None and drift > 2:
                print_warning(
                    f"Clocks differ by {drift}s between the cluster and '{target}'. "
                    "Check NTP on both sides before joining."
                )

            if blocking:
                for reason in blocking:
                    print_error(reason)
                raise typer.Exit(1)

            if t_guests:
                print_warning(
                    f"{len(t_guests)} guest(s) exist on {t_name}. Joining a cluster "
                    "REPLACES its /etc/pve: guests, users, tokens and storages "
                    "defined locally are lost."
                )

            print_warning(
                "This operation cannot be undone through the API: leaving a cluster "
                "requires a reinstall of the node."
            )

            if not yes:
                typed = menu_prompt(f"  Type the target cluster name '{cluster_name}' to confirm")
                if typed != cluster_name:
                    print_error("Name does not match")
                    print_cancelled()
                    raise typer.Exit(1)

            links = {f"link{n}": v for n, v in (("0", link0), ("1", link1)) if v}
            console.print(f"\n[cyan]Joining {t_name} to '{cluster_name}'...[/cyan]")
            try:
                await tc.join_cluster(peer_host, peer_password, fingerprint, **links)
            except PVECliError as e:
                # The API tears the connection down mid-join by design; that is
                # not a failure by itself, the poll below decides.
                print_info(f"Connection dropped during the join ({e}), verifying from the cluster")

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
        return
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)

    # ── Verification from the CLUSTER, on a fresh connection ──
    # The joining node reissues its certificate and restarts pveproxy, so its
    # own answer cannot be trusted here.
    console.print("[cyan]Waiting for the node to appear in the cluster...[/cyan]")
    joined = False
    for _ in range(40):
        await asyncio.sleep(5)
        try:
            async with ProxmoxClient(profile_config) as verify:
                status = await verify.get_cluster_status() or []
                members = [x.get("name") for x in status if x.get("type") == "node"]
                if t_name in members:
                    quorate = next(
                        (x.get("quorate") for x in status if x.get("type") == "cluster"), 0
                    )
                    console.print(f"  members: {', '.join(str(m) for m in members)}")
                    if quorate:
                        joined = True
                        break
        except PVECliError:
            continue

    if joined:
        print_success(f"'{t_name}' joined cluster '{cluster_name}', which is quorate")
        print_info(
            "Update your pvecli profile if you want to reach the cluster through this node"
        )
    else:
        print_error(
            f"'{t_name}' did not show up as a quorate member within the timeout. "
            "Check 'pvecli cluster status' and the node's console before retrying."
        )
        raise typer.Exit(1)


@app.command("status")
@fleet(parallel=True)
@async_to_sync
async def cluster_status(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Show cluster status."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            status = await client.get_cluster_status()

            if not status:
                print_info("No cluster information available")
                return

            table = Table(title="Cluster Status", show_header=True, header_style="bold cyan")
            table.add_column("Type", style="cyan")
            table.add_column("Name")
            table.add_column("Status")
            table.add_column("Nodes", justify="right")
            table.add_column("Quorate")
            table.add_column("Config version")

            for item in status:
                item_type = item.get("type", "-")
                name = item.get("name", "-")
                online = item.get("online", 0)
                nodes = item.get("nodes", 0)
                quorate = item.get("quorate", 0)
                version = item.get("version", "-")

                # Status
                if item_type == "node":
                    status_val = "[green]online[/green]" if online else "[red]offline[/red]"
                else:
                    status_val = "-"

                table.add_row(
                    item_type,
                    name,
                    status_val,
                    str(nodes) if nodes else "-",
                    # Only the cluster entry carries quorum information
                    ("Yes" if quorate else "No") if item_type == "cluster" else "-",
                    str(version) if version != "-" else "-",
                )

            console.print(table)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("resources")
@fleet(parallel=True)
@async_to_sync
async def cluster_resources(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    resource_type: str = typer.Option(
        None, "--type", "-t", help="Filter by type (vm, ct, node, storage)"
    ),
    json_output: bool = JSON_OPTION,
) -> None:
    """Show cluster resources."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)
        profile_name = profile or config_manager.get().default_profile

        async with ProxmoxClient(profile_config) as client:
            resources = await client.get_cluster_resources(resource_type)

            # JSON first: raw API entries, empty list included, no Rich output
            if json_output:
                emit_json(
                    {
                        "profile": profile_name,
                        "type": resource_type,
                        "resources": resources,
                    }
                )
                return

            if not resources:
                print_info("No resources found")
                return

            # Group by type if no filter
            if not resource_type:
                types = {}
                for r in resources:
                    rtype = r.get("type", "unknown")
                    if rtype not in types:
                        types[rtype] = []
                    types[rtype].append(r)

                for rtype, items in types.items():
                    _print_resources_table(items, f"Resources: {rtype}")
            else:
                _print_resources_table(resources, f"Resources: {resource_type}")

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


def _print_resources_table(resources: list[dict], title: str) -> None:
    """Print resources in a table.

    Args:
        resources: List of resources
        title: Table title
    """
    if not resources:
        return

    resource_type = resources[0].get("type", "unknown")

    table = Table(title=title, show_header=True, header_style="bold cyan")

    if resource_type in ["qemu", "lxc"]:
        table.add_column("ID", style="cyan", justify="right")
        table.add_column("Name")
        table.add_column("Node")
        table.add_column("Status")
        table.add_column("CPU", justify="right")
        table.add_column("Memory", justify="right")

        for r in resources:
            vmid = str(r.get("vmid", "-"))
            name = r.get("name", "-")
            node = r.get("node", "-")
            status = r.get("status", "unknown")
            status_color = get_status_color(status)

            if status == "running":
                cpu = r.get("cpu", 0) * 100
                maxcpu = r.get("maxcpu", 1)
                mem = r.get("mem", 0)
                maxmem = r.get("maxmem", 1)
                mem_pct = (mem / maxmem * 100) if maxmem else 0

                cpu_str = f"{format_percentage(cpu)} ({maxcpu})"
                mem_str = f"{format_bytes(mem)} / {format_bytes(maxmem)} ({format_percentage(mem_pct)})"
            else:
                cpu_str = "-"
                mem_str = "-"

            table.add_row(
                vmid,
                name,
                node,
                f"[{status_color}]{status}[/{status_color}]",
                cpu_str,
                mem_str,
            )

    elif resource_type == "node":
        table.add_column("Node", style="cyan")
        table.add_column("Status")
        table.add_column("CPU", justify="right")
        table.add_column("Memory", justify="right")
        table.add_column("Uptime")

        for r in resources:
            node = r.get("node", "-")
            status = r.get("status", "unknown")
            status_color = get_status_color(status)

            cpu = r.get("cpu", 0) * 100
            maxcpu = r.get("maxcpu", 1)
            mem = r.get("mem", 0)
            maxmem = r.get("maxmem", 1)
            mem_pct = (mem / maxmem * 100) if maxmem else 0
            uptime = r.get("uptime", 0)

            table.add_row(
                node,
                f"[{status_color}]{status}[/{status_color}]",
                f"{format_percentage(cpu)} ({maxcpu})",
                f"{format_bytes(mem)} / {format_bytes(maxmem)} ({format_percentage(mem_pct)})",
                format_uptime(uptime) if uptime else "-",
            )

    elif resource_type == "storage":
        table.add_column("Storage", style="cyan")
        table.add_column("Node")
        table.add_column("Type")
        table.add_column("Status")
        table.add_column("Usage", justify="right")

        for r in resources:
            storage = r.get("storage", "-")
            node = r.get("node", "-")
            stype = r.get("type", "-")
            active = r.get("status", "unknown")

            if active == "available":
                status_str = "[green]available[/green]"
            else:
                status_str = f"[red]{active}[/red]"

            disk = r.get("disk", 0)
            maxdisk = r.get("maxdisk", 1)
            disk_pct = (disk / maxdisk * 100) if maxdisk else 0

            table.add_row(
                storage,
                node,
                stype,
                status_str,
                f"{format_bytes(disk)} / {format_bytes(maxdisk)} ({format_percentage(disk_pct)})"
                if maxdisk
                else "-",
            )

    else:
        # Generic table
        table.add_column("ID", style="cyan")
        table.add_column("Type")
        table.add_column("Status")

        for r in resources:
            rid = r.get("id", "-")
            rtype = r.get("type", "-")
            status = r.get("status", "-")

            table.add_row(rid, rtype, status)

    console.print(table)


@app.command("usage")
@fleet(parallel=True)
@async_to_sync
async def cluster_usage(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    no_storage: bool = typer.Option(False, "--no-storage", help="Skip the storage scan (faster)"),
    storage: str = typer.Option(None, "--storage", help="Only scan this storage for guest disks"),
    json_output: bool = JSON_OPTION,
) -> None:
    """Show cluster usage: node load, guest workload, overhead, pools, storage."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)
        profile_name = profile or config_manager.get().default_profile

        async with ProxmoxClient(profile_config) as client:
            await shared_usage(
                client,
                poolid=None,
                include_storage=not no_storage,
                storage_filter=storage,
                as_json=json_output,
                profile_name=profile_name,
            )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("tasks")
@fleet(parallel=True)
@async_to_sync
async def cluster_tasks(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    running: bool = typer.Option(False, "--running", "-r", help="Only show running tasks"),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum number of tasks"),
) -> None:
    """Show cluster tasks."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            tasks = await client.get_cluster_tasks(running=running, limit=limit)

            if not tasks:
                print_info("No tasks found")
                return

            table = Table(
                title="Cluster Tasks" if not running else "Running Tasks",
                show_header=True,
                header_style="bold cyan",
            )
            table.add_column("Node", style="cyan")
            table.add_column("Type")
            table.add_column("ID")
            table.add_column("User")
            table.add_column("Status")
            table.add_column("Start Time")

            for task in tasks:
                node = task.get("node", "-")
                task_type = task.get("type", "-")
                task_id = task.get("id", "-")
                user = task.get("user", "-")
                status = task.get("status", "unknown")

                # Format start time
                starttime = task.get("starttime", 0)
                if starttime:
                    from datetime import datetime

                    start_str = datetime.fromtimestamp(starttime).strftime("%Y-%m-%d %H:%M")
                else:
                    start_str = "-"

                # Status color
                if status == "running":
                    status_str = "[yellow]running[/yellow]"
                elif "OK" in status:
                    status_str = "[green]OK[/green]"
                else:
                    status_str = f"[red]{status}[/red]"

                table.add_row(node, task_type, task_id, user, status_str, start_str)

            console.print(table)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Cluster shutdown / reboot orchestration
# ---------------------------------------------------------------------------

_CEPH_FLAGS = ["noout", "nobackfill", "norecover", "norebalance"]


async def _orchestrate_cluster_power(
    client: ProxmoxClient,
    profile_host: str,
    command: str,
    yes: bool,
    skip_ceph: bool,
    skip_ha: bool,
    stopall_timeout: int,
) -> None:
    """Orchestrate a full cluster shutdown or reboot."""
    action = "Shutdown" if command == "shutdown" else "Reboot"

    # Track what was done for interrupt recovery messages
    ha_disabled: list[str] = []
    ha_disarmed = False
    ceph_flags_set = False

    try:
        # ── Phase 0: Gather info ──────────────────────────────────────
        nodes = await client.get_nodes()
        online_nodes = [n for n in nodes if n.get("status") == "online"]
        if not online_nodes:
            print_error("No online nodes found")
            raise typer.Exit(1)

        connected_node = await detect_connected_node(client, profile_host)

        # Count running guests per node
        resources = await client.get_cluster_resources(resource_type="vm")
        running_per_node: dict[str, int] = {}
        for r in resources:
            if r.get("status") == "running":
                n = r.get("node", "")
                running_per_node[n] = running_per_node.get(n, 0) + 1

        # Display summary table
        table = Table(
            title=f"Cluster {action} Plan",
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Node", style="cyan")
        table.add_column("Status")
        table.add_column("Running Guests", justify="right")
        table.add_column("Role")

        # Order: workers first, connected node last
        ordered = sorted(
            online_nodes,
            key=lambda n: (n.get("node") == connected_node, n.get("node", "")),
        )

        for n in ordered:
            name = n.get("node", "")
            guests = running_per_node.get(name, 0)
            role = (
                "[bold yellow]connected (last)[/bold yellow]"
                if name == connected_node
                else ""
            )
            table.add_row(
                name,
                "[green]online[/green]",
                str(guests) if guests else "-",
                role,
            )

        console.print()
        console.print(table)
        total_guests = sum(running_per_node.values())
        console.print(
            f"\n[bold red]{action} will affect {len(online_nodes)} node(s) "
            f"and {total_guests} running guest(s).[/bold red]"
        )
        if command == "shutdown":
            console.print("[dim]Nodes will NOT come back automatically.[/dim]")

        # ── Phase 1: Double confirmation ──────────────────────────────
        if not yes:
            if not confirm(f"{action} the entire cluster?", default=False):
                print_cancelled()
                return

            typed = Prompt.ask(
                f"[bold red]Type '{action.upper()}' to confirm[/bold red]"
            )
            if typed != action.upper():
                print_cancelled()
                return

        # ── Phase 2: Disable HA ───────────────────────────────────────
        # PVE 9.2+ disarms the whole HA stack in one call: resources are
        # frozen where they are and every watchdog is released, so no node
        # fences itself while its peers go down. Older versions lack the
        # endpoint and fall back on disabling each resource.
        if not skip_ha:
            try:
                ha_resources = await client.get_ha_resources()
            except PVECliError:
                ha_resources = []  # reported by the fallback below
            if ha_resources:
                try:
                    await client.disarm_ha("freeze")
                    ha_disarmed = True
                    print_success(
                        f"HA stack disarmed, {len(ha_resources)} resource(s) frozen in place"
                    )
                except PVECliError as e:
                    if not is_not_implemented(e):
                        print_warning(f"Could not disarm HA ({e}), disabling resources instead")
        if not skip_ha and not ha_disarmed:
            try:
                ha_resources = await client.get_ha_resources()
                active_ha = [
                    r
                    for r in ha_resources
                    if r.get("state") in ("started", "enabled")
                ]
                if active_ha:
                    console.print(
                        f"\n[bold cyan]── Disabling {len(active_ha)} HA resource(s) ──[/bold cyan]"
                    )
                    for r in active_ha:
                        sid = r.get("sid", "")
                        with Progress(
                            SpinnerColumn(),
                            TextColumn("[progress.description]{task.description}"),
                            console=console,
                        ) as progress:
                            progress.add_task(
                                description=f"Disabling HA resource {sid}...",
                                total=None,
                            )
                            await client.disable_ha_resource(sid)
                        ha_disabled.append(sid)
                        print_success(f"HA resource {sid} disabled")
                else:
                    print_info("No active HA resources found")
            except PVECliError:
                print_info("HA not configured or not accessible, skipping")

        # ── Phase 3: Ceph flags ───────────────────────────────────────
        if not skip_ceph:
            ceph_node = connected_node or ordered[0].get("node", "")

            # Detection and flag setting are kept apart on purpose: a failure
            # to set the flags must never be reported as "Ceph not detected".
            ceph_present = True
            try:
                await client.get_ceph_status(ceph_node)
            except PVECliError as e:
                if not ceph_absent(e):
                    # Could not tell. Skipping here would silently shut a Ceph
                    # cluster down without its flags, so stop and let the
                    # operator decide.
                    print_error(f"Could not determine whether Ceph is present: {e}")
                    print_info("Pass --skip-ceph to proceed anyway (not recommended).")
                    raise typer.Exit(1)
                ceph_present = False
                print_info("Ceph not detected, skipping flag management")

            if ceph_present:
                console.print(
                    "\n[bold cyan]── Setting Ceph maintenance flags ──[/bold cyan]"
                )
                try:
                    for flag in _CEPH_FLAGS:
                        with Progress(
                            SpinnerColumn(),
                            TextColumn("[progress.description]{task.description}"),
                            console=console,
                        ) as progress:
                            progress.add_task(
                                description=f"Setting Ceph flag: {flag}...",
                                total=None,
                            )
                            await client.set_ceph_flag(flag)
                except PVECliError as e:
                    # Some flags may already be set: report what was reached
                    print_error(f"Failed to set Ceph maintenance flags: {e}")
                    print_error(
                        "Aborting: shutting down a Ceph cluster without these flags "
                        "lets Ceph rebalance while nodes go down one by one."
                    )
                    print_info("Pass --skip-ceph to proceed anyway (not recommended).")
                    raise typer.Exit(1)
                ceph_flags_set = True
                print_success(f"Ceph flags set: {', '.join(_CEPH_FLAGS)}")

        # ── Phase 4: Stop all guests ──────────────────────────────────
        console.print("\n[bold cyan]── Stopping all guests ──[/bold cyan]")

        node_names = [n.get("node", "") for n in ordered]
        for name in node_names:
            guests = running_per_node.get(name, 0)
            if guests == 0:
                print_info(f"No running guests on '{name}', skipping stopall")
                continue

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task(
                    description=f"Stopping {guests} guest(s) on '{name}'...",
                    total=None,
                )
                upid = await client.stopall_node(name, timeout=stopall_timeout)
                await client.wait_for_task(
                    name, upid, timeout=stopall_timeout + 60
                )
            print_success(f"All guests stopped on '{name}'")

        # ── Phase 5: Shutdown/reboot nodes ────────────────────────────
        console.print(f"\n[bold cyan]── Sending {command} to nodes ──[/bold cyan]")

        # Workers first (all except connected)
        for n in ordered:
            name = n.get("node", "")
            if name == connected_node:
                continue
            await client.node_command(name, command)
            print_success(f"{action} command sent to '{name}'")
            await asyncio.sleep(2)

        # Connected node last
        if connected_node:
            console.print(
                f"\n[bold yellow]About to {command} the connected node "
                f"'{connected_node}'. CLI access will be lost.[/bold yellow]"
            )
            await client.node_command(connected_node, command)
            print_success(f"{action} command sent to '{connected_node}'")
        elif ordered:
            # Could not detect connected node: send to last in order
            last = ordered[-1].get("node", "")
            await client.node_command(last, command)
            print_success(f"{action} command sent to '{last}'")

        # ── Post-action reminders ─────────────────────────────────────
        # Both paths set the same flags, and shutdown is the one where the
        # operator is most likely to forget: a noout left behind on the way
        # back up hides a failed OSD instead of rebalancing around it.
        console.print()
        reminders = []
        if ceph_flags_set:
            reminders.append(
                "Unset Ceph flags: "
                + ", ".join(f"ceph osd unset {f}" for f in _CEPH_FLAGS)
            )
        if ha_disarmed:
            reminders.append("Re-arm HA: pvecli cluster ha arm")
        if ha_disabled:
            reminders.append(
                "Re-enable HA resources: " + ", ".join(ha_disabled)
            )
        if reminders:
            when = "reboot" if command == "reboot" else "the nodes are back up"
            console.print(
                f"[bold yellow]── After {when}, remember to: ──[/bold yellow]"
            )
            for r in reminders:
                console.print(f"  • {r}")
                console.print()

        print_success(f"Cluster {command} complete.")

    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print()
        print_warning("Interrupted!")
        if ha_disarmed:
            print_warning("The HA stack was disarmed. Re-arm it with: pvecli cluster ha arm")
        if ha_disabled:
            print_warning(f"HA resources were disabled: {', '.join(ha_disabled)}")
            print_warning("You may need to re-enable them manually.")
        if ceph_flags_set:
            print_warning(f"Ceph flags were set: {', '.join(_CEPH_FLAGS)}")
            print_warning("You may need to unset them manually.")
        raise typer.Exit(1)


@app.command("shutdown")
@async_to_sync
async def cluster_shutdown(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    skip_ceph: bool = typer.Option(
        False, "--skip-ceph", help="Skip setting Ceph OSD flags"
    ),
    skip_ha: bool = typer.Option(
        False, "--skip-ha", help="Skip disabling HA resources"
    ),
    timeout: int = typer.Option(
        300, "--timeout", "-t", help="Timeout for stopping guests per node (seconds)"
    ),
) -> None:
    """Shutdown the entire cluster (all nodes).

    Orchestrates a safe cluster shutdown:
    1. Disarms HA (PVE 9.2+), or disables each HA resource on older versions
    2. Sets Ceph maintenance flags if Ceph is detected
    3. Stops all guests on each node
    4. Shuts down nodes (connected node last)
    """
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            await _orchestrate_cluster_power(
                client,
                profile_config.host,
                command="shutdown",
                yes=yes,
                skip_ceph=skip_ceph,
                skip_ha=skip_ha,
                stopall_timeout=timeout,
            )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("reboot")
@async_to_sync
async def cluster_reboot(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    skip_ceph: bool = typer.Option(
        False, "--skip-ceph", help="Skip setting Ceph OSD flags"
    ),
    skip_ha: bool = typer.Option(
        False, "--skip-ha", help="Skip disabling HA resources"
    ),
    timeout: int = typer.Option(
        300, "--timeout", "-t", help="Timeout for stopping guests per node (seconds)"
    ),
) -> None:
    """Reboot the entire cluster (all nodes).

    Orchestrates a safe cluster reboot:
    1. Disarms HA (PVE 9.2+), or disables each HA resource on older versions
    2. Sets Ceph maintenance flags if Ceph is detected
    3. Stops all guests on each node
    4. Reboots nodes (connected node last)
    """
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            await _orchestrate_cluster_power(
                client,
                profile_config.host,
                command="reboot",
                yes=yes,
                skip_ceph=skip_ceph,
                skip_ha=skip_ha,
                stopall_timeout=timeout,
            )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# ── cluster ha ───────────────────────────────────────────────────────────

ha_app = typer.Typer(help="Arm or disarm the whole HA stack (PVE 9.2+)", no_args_is_help=True)
app.add_typer(ha_app, name="ha")

_HA_ARM_UNSUPPORTED = (
    "Arming and disarming the HA stack needs PVE 9.2 or later. On this cluster, "
    "disable the resources one by one instead: pvecli vm ha set <id> --state disabled"
)


@ha_app.command("disarm")
@async_to_sync
async def ha_disarm(
    mode: str = typer.Option(
        "freeze",
        "--mode",
        "-m",
        help="freeze: keep every HA resource where it is. ignore: let them be managed by hand",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", is_flag=True, help="Skip confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Disarm HA cluster wide: release every watchdog before maintenance."""
    if mode not in ("freeze", "ignore"):
        print_error("--mode must be 'freeze' or 'ignore'")
        raise typer.Exit(1)
    config_manager = ConfigManager()
    try:
        profile_config = config_manager.get_profile(profile)
        async with ProxmoxClient(profile_config) as client:
            if not yes and not confirm(
                f"Disarm HA on this cluster (resource mode: {mode})? "
                "No node will fence and no guest will be recovered until it is re-armed.",
                default=False,
            ):
                print_cancelled()
                return
            try:
                await client.disarm_ha(mode)
            except PVECliError as e:
                if is_not_implemented(e):
                    print_error(_HA_ARM_UNSUPPORTED)
                    raise typer.Exit(1)
                raise
            print_success(f"HA disarmed ({mode}). Re-arm it with: pvecli cluster ha arm")
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@ha_app.command("arm")
@async_to_sync
async def ha_arm(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Re-arm HA after a disarm."""
    config_manager = ConfigManager()
    try:
        profile_config = config_manager.get_profile(profile)
        async with ProxmoxClient(profile_config) as client:
            try:
                await client.arm_ha()
            except PVECliError as e:
                if is_not_implemented(e):
                    print_error(_HA_ARM_UNSUPPORTED)
                    raise typer.Exit(1)
                raise
            print_success("HA re-armed: check its state with: pvecli cluster health")
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# ── cluster guests ───────────────────────────────────────────────────────

guests_app = typer.Typer(
    help="Start, shut down, suspend or migrate many guests in one go",
    no_args_is_help=True,
)
app.add_typer(guests_app, name="guests")


async def _select_guests(
    client: ProxmoxClient,
    vms: str | None,
    tag: str | None,
    pool: str | None,
    node: str | None,
) -> list[dict]:
    """Guests matching every filter given. At least one filter is required:
    acting on the whole cluster by accident is not an option."""
    if not any((vms, tag, pool, node)):
        raise PVECliError("Pick the guests with --vms, --tag, --pool or --node")
    from ._shared import parse_id_list

    wanted = set(parse_id_list(vms, "guest")) if vms else None
    guests = []
    for r in await client.get_cluster_resources(resource_type="vm"):
        if r.get("template"):
            continue
        if wanted is not None and r.get("vmid") not in wanted:
            continue
        if tag and tag not in parse_tags(r.get("tags", "") or ""):
            continue
        if pool and r.get("pool") != pool:
            continue
        if node and r.get("node") != node:
            continue
        guests.append(r)
    if wanted:
        missing = wanted - {g.get("vmid") for g in guests}
        if missing:
            raise PVECliError(
                "Not found or filtered out: " + ", ".join(str(m) for m in sorted(missing))
            )
    if not guests:
        raise PVECliError("No guest matches these filters")
    return sorted(guests, key=lambda g: g.get("vmid", 0))


async def _run_bulk(
    client: ProxmoxClient,
    action: str,
    guests: list[dict],
    yes: bool,
    cluster_params: dict,
    node_params: dict,
    describe: str,
) -> None:
    """Run one bulk action: a single cluster task where PVE offers it, one
    task per node (startall/stopall/suspendall/migrateall) otherwise.

    Beware of suspendall: PVE always hibernates to disk there (todisk => 1),
    so a plain in-memory suspend falls back on one call per VM instead.
    """
    table = Table(title=f"{describe}: {len(guests)} guest(s)", header_style="bold cyan")
    table.add_column("ID", justify="right", style="cyan")
    table.add_column("Name")
    table.add_column("Type")
    table.add_column("Node")
    table.add_column("Status")
    for g in guests:
        table.add_row(
            str(g.get("vmid")), g.get("name", "") or "-",
            "VM" if g.get("type") == "qemu" else "CT", g.get("node", ""), g.get("status", ""),
        )
    console.print(table)
    if not yes and not confirm(f"{describe} these {len(guests)} guest(s)?", default=False):
        print_cancelled()
        return

    ids = [g["vmid"] for g in guests]
    params = {k: v for k, v in cluster_params.items() if v is not None}
    try:
        upid = await client.bulk_guest_action(action, vms=ids, **params)
        tasks = [("", upid)]  # wait_for_task reads the node from the UPID
        print_info("Running as one cluster task")
    except PVECliError as e:
        if not is_not_implemented(e):
            raise
        if action == "suspend" and not cluster_params.get("to-disk"):
            tasks = [
                (g.get("node", ""), await client.suspend_vm(g.get("node", ""), g["vmid"]))
                for g in guests
            ]
            print_info(f"Running as {len(tasks)} task(s), one per VM")
            return await _finish_bulk(client, action, guests, tasks, cluster_params, describe)
        # Older PVE: one task per node, same filtering through 'vms'
        node_action = {
            "start": "startall", "shutdown": "stopall",
            "suspend": "suspendall", "migrate": "migrateall",
        }[action]
        per_node: dict[str, list[int]] = {}
        for g in guests:
            per_node.setdefault(g.get("node", ""), []).append(g["vmid"])
        tasks = []
        for node, node_ids in sorted(per_node.items()):
            p = {k: v for k, v in node_params.items() if v is not None}
            p["vms"] = ",".join(str(i) for i in node_ids)
            if node_action == "migrateall":
                # migrateall refuses to run without a worker count unless
                # datacenter.cfg sets one; 1 is the web UI default
                p.setdefault("max-workers", 1)

            async def call(params: dict) -> str:
                if node_action == "migrateall":
                    return await client.migrate_all(node, **params)
                return await client.node_bulk_action(node, node_action, **params)

            # The worker count is spelled 'max-workers', 'maxworkers' on
            # older releases, and some per-node endpoints of those releases
            # accept neither: try each, then run without it.
            workers = p.pop("max-workers", None)
            upid = None
            for key in ("max-workers", "maxworkers", None) if workers is not None else (None,):
                attempt = {**p, key: workers} if key else p
                try:
                    upid = await call(attempt)
                    break
                except PVECliError as e:
                    if key is None or "not defined in schema" not in str(e):
                        raise
            if workers is not None and key is None:
                print_warning(
                    f"{node}: this PVE version cannot limit the parallel tasks of "
                    f"{node_action}, its own default applies"
                )
            tasks.append((node, upid))
        print_info(f"Running as {len(tasks)} task(s), one per node")

    await _finish_bulk(client, action, guests, tasks, cluster_params, describe)


async def _finish_bulk(
    client: ProxmoxClient,
    action: str,
    guests: list[dict],
    tasks: list[tuple[str, str]],
    cluster_params: dict,
    describe: str,
) -> None:
    """Wait for the bulk tasks, then check each guest reached the state."""
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console
    ) as progress:
        progress.add_task(f"{describe} {len(guests)} guest(s)...", total=None)
        results = await asyncio.gather(
            *(client.wait_for_task(n, u, timeout=7200) for n, u in tasks),
            return_exceptions=True,
        )
    # "WARNINGS: n" is how PVE ends a task that succeeded with warnings, a
    # skipped guest for instance: not a failure, the check below tells which.
    errors = [r for r in results if isinstance(r, Exception)]
    failed = [str(r) for r in errors if "WARNINGS:" not in str(r)]
    if failed:
        for f in failed:
            print_error(f)
        raise typer.Exit(1)
    if errors:
        print_warning("The task ended with warnings, see: pvecli cluster tasks")

    # A bulk task ends OK even when it skipped guests (locked, HA-managed,
    # already in the target state...): check every guest really got there.
    missed = await _bulk_missed(client, action, guests, cluster_params.get("target"))
    if missed:
        print_warning(
            f"{describe}: {len(guests) - len(missed)}/{len(guests)} guest(s) done. "
            "Not reached: " + "; ".join(missed)
        )
        print_info("The task log says why: pvecli cluster tasks")
        raise typer.Exit(1)
    print_success(f"{describe}: done for {len(guests)} guest(s)")


async def _bulk_missed(
    client: ProxmoxClient, action: str, guests: list[dict], target: str | None
) -> list[str]:
    """Guests not in the state the bulk action promised, read live."""

    async def check(g: dict) -> str | None:
        kind = "qemu" if g.get("type") == "qemu" else "lxc"
        node = target if action == "migrate" else g.get("node")
        try:
            cur = await client.get(f"/nodes/{node}/{kind}/{g['vmid']}/status/current") or {}
        except PVECliError:
            return f"{g['vmid']} (still on {g.get('node')})" if action == "migrate" else f"{g['vmid']} (unreadable)"
        status, lock = cur.get("status"), cur.get("lock")
        ok = {
            "start": status == "running",
            "shutdown": status == "stopped",
            "suspend": cur.get("qmpstatus") in ("paused", "suspended") or lock == "suspended",
            "migrate": True,
        }[action]
        if ok:
            return None
        return f"{g['vmid']} ({status}{', locked ' + lock if lock else ''})"

    results = await asyncio.gather(*(check(g) for g in guests))
    return [r for r in results if r]


_GUEST_FILTERS = {
    "vms": typer.Option(None, "--vms", help="Guest IDs: 100,101 or 100-110"),
    "tag": typer.Option(None, "--tag", help="Only guests carrying this tag"),
    "pool": typer.Option(None, "--pool", help="Only guests of this resource pool"),
    "node": typer.Option(None, "--node", help="Only guests currently on this node"),
}


@guests_app.command("start")
@async_to_sync
async def guests_start(
    vms: str = _GUEST_FILTERS["vms"],
    tag: str = _GUEST_FILTERS["tag"],
    pool: str = _GUEST_FILTERS["pool"],
    node: str = _GUEST_FILTERS["node"],
    max_workers: int = typer.Option(None, "--max-workers", help="Guests handled at the same time"),
    yes: bool = typer.Option(False, "--yes", "-y", is_flag=True, help="Skip confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Start (or resume) every guest matching the filters."""
    await _guests_command(
        "start", vms, tag, pool, node, yes, profile, "Start",
        {"max-workers": max_workers},
        # startall only starts onboot=1 guests unless forced
        {"max-workers": max_workers, "force": 1},
    )


@guests_app.command("shutdown")
@async_to_sync
async def guests_shutdown(
    vms: str = _GUEST_FILTERS["vms"],
    tag: str = _GUEST_FILTERS["tag"],
    pool: str = _GUEST_FILTERS["pool"],
    node: str = _GUEST_FILTERS["node"],
    timeout: int = typer.Option(None, "--timeout", "-t", help="Shutdown timeout per guest, in seconds"),
    force_stop: bool = typer.Option(False, "--force-stop", is_flag=True, help="Hard stop guests still running after the timeout"),
    max_workers: int = typer.Option(None, "--max-workers", help="Guests handled at the same time"),
    yes: bool = typer.Option(False, "--yes", "-y", is_flag=True, help="Skip confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Shut down every guest matching the filters."""
    params = {"max-workers": max_workers, "timeout": timeout, "force-stop": 1 if force_stop else None}
    await _guests_command("shutdown", vms, tag, pool, node, yes, profile, "Shut down", params, params)


@guests_app.command("suspend")
@async_to_sync
async def guests_suspend(
    vms: str = _GUEST_FILTERS["vms"],
    tag: str = _GUEST_FILTERS["tag"],
    pool: str = _GUEST_FILTERS["pool"],
    node: str = _GUEST_FILTERS["node"],
    to_disk: bool = typer.Option(False, "--to-disk", is_flag=True, help="Hibernate to disk instead of pausing in memory"),
    max_workers: int = typer.Option(None, "--max-workers", help="Guests handled at the same time"),
    yes: bool = typer.Option(False, "--yes", "-y", is_flag=True, help="Skip confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Suspend every VM matching the filters (containers cannot be suspended)."""
    await _guests_command(
        "suspend", vms, tag, pool, node, yes, profile, "Suspend",
        {"max-workers": max_workers, "to-disk": 1 if to_disk else None},
        {"max-workers": max_workers},
        only_vms=True,
    )


@guests_app.command("migrate")
@async_to_sync
async def guests_migrate(
    target: str = typer.Argument(..., help="Target node"),
    vms: str = _GUEST_FILTERS["vms"],
    tag: str = _GUEST_FILTERS["tag"],
    pool: str = _GUEST_FILTERS["pool"],
    node: str = _GUEST_FILTERS["node"],
    with_local_disks: bool = typer.Option(False, "--with-local-disks", is_flag=True, help="Copy local disks during live migrations"),
    max_workers: int = typer.Option(None, "--max-workers", help="Migrations running at the same time"),
    yes: bool = typer.Option(False, "--yes", "-y", is_flag=True, help="Skip confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Migrate every guest matching the filters to one node."""
    params = {
        "target": target,
        "max-workers": max_workers,
        "with-local-disks": 1 if with_local_disks else None,
    }
    await _guests_command(
        "migrate", vms, tag, pool, node, yes, profile, f"Migrate to {target}",
        {**params, "online": 1}, params, exclude_node=target,
    )


async def _guests_command(
    action: str,
    vms: str | None,
    tag: str | None,
    pool: str | None,
    node: str | None,
    yes: bool,
    profile: str | None,
    describe: str,
    cluster_params: dict,
    node_params: dict,
    only_vms: bool = False,
    exclude_node: str | None = None,
) -> None:
    config_manager = ConfigManager()
    try:
        profile_config = config_manager.get_profile(profile)
        async with ProxmoxClient(profile_config) as client:
            guests = await _select_guests(client, vms, tag, pool, node)
            if only_vms:
                skipped = [g for g in guests if g.get("type") != "qemu"]
                guests = [g for g in guests if g.get("type") == "qemu"]
                if skipped:
                    print_warning(
                        "Containers cannot be suspended, skipped: "
                        + ", ".join(str(g.get("vmid")) for g in skipped)
                    )
            if exclude_node:
                members = {n.get("node") for n in await client.get_nodes()}
                if exclude_node not in members:
                    raise PVECliError(f"Unknown node '{exclude_node}'")
                guests = [g for g in guests if g.get("node") != exclude_node]
            if not guests:
                print_info("Nothing to do")
                return
            await _run_bulk(client, action, guests, yes, cluster_params, node_params, describe)
    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
