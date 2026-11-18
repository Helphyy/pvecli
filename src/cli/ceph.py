"""Ceph read commands.

Deployment and destruction stay out of pvecli on purpose: installing the
packages has no REST endpoint, and destroying a pool or an OSD is not
something a multi-target CLI should make easy. Maintenance flags are the
only write path, and they live in `cluster shutdown`.
"""

import typer
from rich.table import Table

from ..api.client import ProxmoxClient
from ..api.exceptions import PVECliError
from ..config import ConfigManager
from ..utils import (
    JSON_OPTION,
    console,
    emit_json,
    format_bytes,
    format_percentage,
    print_error,
    print_info,
    print_warning,
)
from ..utils.fleet import fleet
from ..utils.helpers import async_to_sync, ordered_group
from ._shared import ceph_absent as _absent


def _no_ceph(json_output: bool = False) -> None:
    """A cluster without Ceph is an answer, not an error: in --json it is a
    valid document on stdout, never a sentence that breaks the stream."""
    if json_output:
        emit_json({"ceph": None})
    else:
        print_info("No Ceph on this cluster")


app = typer.Typer(
    help="Inspect Ceph (read only)",
    no_args_is_help=True,
    cls=ordered_group(["status", "osd", "pool", "flags", "versions", "log"]),
)

_HEALTH_STYLE = {
    "HEALTH_OK": "[green]HEALTH_OK[/green]",
    "HEALTH_WARN": "[yellow]HEALTH_WARN[/yellow]",
    "HEALTH_ERR": "[red]HEALTH_ERR[/red]",
}


async def _first_node(client: ProxmoxClient) -> str:
    nodes = sorted(
        n.get("node", "")
        for n in await client.get_nodes() or []
        if n.get("status") == "online"
    )
    if not nodes:
        print_error("No online node found")
        raise typer.Exit(1)
    return nodes[0]


@app.command("status")
@fleet(parallel=True)
@async_to_sync
async def ceph_status(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    json_output: bool = JSON_OPTION,
) -> None:
    """Overall Ceph health, monitors, OSDs and capacity."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            try:
                status = await client.get_ceph_cluster_status() or {}
            except PVECliError as e:
                if _absent(e):
                    _no_ceph(json_output)
                    return
                raise

            health = (status.get("health") or {}).get("status", "?")
            monmap = status.get("monmap") or {}
            osdmap = status.get("osdmap") or {}
            if isinstance(osdmap.get("osdmap"), dict):
                osdmap = osdmap["osdmap"]
            pgmap = status.get("pgmap") or {}

            # A flag left behind hides real failures, so surface it here
            try:
                flags = await client.get_ceph_flags() or []
            except PVECliError:
                flags = []
            active_flags = [f.get("name") for f in flags if f.get("value")]

            if json_output:
                emit_json({
                    "health": health,
                    "checks": list((status.get("health") or {}).get("checks", {})),
                    "monitors": len(monmap.get("mons") or []),
                    "osds": {
                        "total": osdmap.get("num_osds"),
                        "up": osdmap.get("num_up_osds"),
                        "in": osdmap.get("num_in_osds"),
                    },
                    "capacity": {
                        "total": pgmap.get("bytes_total"),
                        "used": pgmap.get("bytes_used"),
                        "avail": pgmap.get("bytes_avail"),
                    },
                    "active_flags": active_flags,
                })
                return

            table = Table(title="Ceph status", show_header=True, header_style="bold cyan")
            table.add_column("Item", style="cyan")
            table.add_column("Value")

            table.add_row("Health", _HEALTH_STYLE.get(health, health))
            for name, check in ((status.get("health") or {}).get("checks") or {}).items():
                summary = (check.get("summary") or {}).get("message", "")
                table.add_row(f"  {name}", f"[yellow]{summary}[/yellow]")

            mons = monmap.get("mons") or []
            table.add_row("Monitors", str(len(mons)) if mons else "-")

            total, up, inn = (
                osdmap.get("num_osds"),
                osdmap.get("num_up_osds"),
                osdmap.get("num_in_osds"),
            )
            if total is not None:
                mismatch = (up is not None and up != total) or (
                    inn is not None and inn != total
                )
                osd_text = f"{up}/{total} up, {inn}/{total} in"
                # An osdmap slot with no daemon (a destroyed OSD not purged)
                # makes the counts differ while Ceph itself reports no problem,
                # so trust the health checks rather than the raw numbers.
                if mismatch and health == "HEALTH_OK":
                    table.add_row(
                        "OSDs",
                        f"[yellow]{osd_text}[/yellow] (health is OK: likely an "
                        "empty osdmap slot, purge it with ceph osd purge)",
                    )
                elif mismatch:
                    table.add_row("OSDs", f"[red]{osd_text}[/red]")
                else:
                    table.add_row("OSDs", f"[green]{osd_text}[/green]")

            bt, bu, ba = (
                pgmap.get("bytes_total"),
                pgmap.get("bytes_used"),
                pgmap.get("bytes_avail"),
            )
            if bt:
                table.add_row(
                    "Capacity",
                    f"{format_bytes(bu or 0)} used of {format_bytes(bt)} "
                    f"({format_percentage((bu or 0) / bt * 100)}), {format_bytes(ba or 0)} free",
                )
            if pgmap.get("num_pgs"):
                table.add_row("Placement groups", str(pgmap.get("num_pgs")))

            table.add_row(
                "Flags",
                f"[yellow]{', '.join(str(f) for f in active_flags)}[/yellow]"
                if active_flags
                else "[green]none set[/green]",
            )
            console.print(table)

            if active_flags:
                print_warning(
                    "Flags are set on this cluster. A forgotten noout hides a failed "
                    "OSD for as long as it stays on."
                )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("osd")
@fleet(parallel=True)
@async_to_sync
async def ceph_osd(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    json_output: bool = JSON_OPTION,
) -> None:
    """OSD tree with usage and device class."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            node = await _first_node(client)
            try:
                tree = await client.get_ceph_osds(node) or {}
            except PVECliError as e:
                if _absent(e):
                    _no_ceph(json_output)
                    return
                raise

            root = tree.get("root") if isinstance(tree, dict) else None
            entries = (root or {}).get("children")

            # The tree is nested; walk it and keep the OSD leaves
            osds: list[dict] = []

            def walk(nodes: list[dict] | None, host: str = "") -> None:
                for item in nodes or []:
                    if item.get("type") == "osd":
                        osds.append({**item, "host": host})
                    walk(item.get("children"), item.get("name", host))

            walk(entries if entries is not None else (tree.get("children") or []))

            if not osds:
                print_info("No OSD reported")
                return

            if json_output:
                emit_json(osds)
                return

            table = Table(title="Ceph OSDs", show_header=True, header_style="bold cyan")
            table.add_column("OSD", style="cyan", justify="right")
            table.add_column("Host")
            table.add_column("Status")
            table.add_column("In")
            table.add_column("Size", justify="right")
            table.add_column("Used", justify="right")
            table.add_column("Class")

            for o in sorted(osds, key=lambda x: x.get("id", 0)):
                status = o.get("status", "?")
                inout = "in" if o.get("in") else "out"
                total = o.get("total_space") or 0
                used_pct = o.get("percent_used")
                table.add_row(
                    f"osd.{o.get('id')}",
                    o.get("host", "-"),
                    f"[green]{status}[/green]" if status == "up" else f"[red]{status}[/red]",
                    f"[green]{inout}[/green]" if inout == "in" else f"[yellow]{inout}[/yellow]",
                    format_bytes(total) if total else "-",
                    format_percentage(used_pct) if used_pct is not None else "-",
                    o.get("device_class", "-"),
                )
            console.print(table)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("pool")
@fleet(parallel=True)
@async_to_sync
async def ceph_pool(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    json_output: bool = JSON_OPTION,
) -> None:
    """Ceph pools, their replication settings and their usage."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            node = await _first_node(client)
            try:
                pools = await client.get_ceph_pools(node) or []
            except PVECliError as e:
                if _absent(e):
                    _no_ceph(json_output)
                    return
                raise

            if not pools:
                print_info("No Ceph pool defined")
                return

            if json_output:
                emit_json(pools)
                return

            table = Table(title="Ceph pools", show_header=True, header_style="bold cyan")
            table.add_column("Pool", style="cyan")
            table.add_column("Size", justify="right")
            table.add_column("Min size", justify="right")
            table.add_column("PGs", justify="right")
            table.add_column("Used", justify="right")
            table.add_column("Rule")

            risky = []
            for p in sorted(pools, key=lambda x: x.get("pool_name", "")):
                min_size = p.get("min_size")
                unsafe = min_size is not None and min_size < 2
                if unsafe:
                    risky.append(p.get("pool_name"))
                table.add_row(
                    p.get("pool_name", "-"),
                    str(p.get("size", "-")),
                    f"[red]{min_size}[/red]" if unsafe else str(min_size if min_size is not None else "-"),
                    str(p.get("pg_num", "-")),
                    format_bytes(p.get("bytes_used", 0)),
                    str(p.get("crush_rule_name") or p.get("crush_rule", "-")),
                )
            console.print(table)

            if risky:
                print_warning(
                    f"min_size below 2 on: {', '.join(str(r) for r in risky)}. "
                    "A single OSD failure can then lose writes."
                )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("flags")
@fleet(parallel=True)
@async_to_sync
async def ceph_flags(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    json_output: bool = JSON_OPTION,
) -> None:
    """Ceph OSD flags and which ones are currently set."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            try:
                flags = await client.get_ceph_flags() or []
            except PVECliError as e:
                if _absent(e):
                    _no_ceph(json_output)
                    return
                raise

            if json_output:
                emit_json(flags)
                return

            table = Table(title="Ceph flags", show_header=True, header_style="bold cyan")
            table.add_column("Flag", style="cyan")
            table.add_column("Set")
            table.add_column("Meaning")
            for f in sorted(flags, key=lambda x: x.get("name", "")):
                on = bool(f.get("value"))
                table.add_row(
                    f.get("name", "-"),
                    "[yellow]yes[/yellow]" if on else "[dim]no[/dim]",
                    f.get("description") or "",
                )
            console.print(table)

            active = [f.get("name") for f in flags if f.get("value")]
            if active:
                print_warning(
                    f"Set right now: {', '.join(str(a) for a in active)}. "
                    "Clear them once maintenance is over."
                )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("versions")
@fleet(parallel=True)
@async_to_sync
async def ceph_versions(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    json_output: bool = JSON_OPTION,
) -> None:
    """Ceph version of every daemon, to spot the ones left behind."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            try:
                # Without a scope the endpoint reports every daemon type,
                # which is what makes stragglers visible after an upgrade.
                meta = await client.get_ceph_metadata() or {}
            except PVECliError as e:
                if _absent(e):
                    _no_ceph(json_output)
                    return
                raise

            # Without Ceph the endpoint answers {"node": {}} instead of erroring
            if not any(v for v in (meta or {}).values()):
                _no_ceph(json_output)
                return

            if json_output:
                emit_json(meta)
                return

            table = Table(title="Ceph versions", show_header=True, header_style="bold cyan")
            table.add_column("Scope", style="cyan")
            table.add_column("Name")
            table.add_column("Version")

            # Shape is {scope: {name: {version: {str: "19.2.5"}, ...}}}
            distinct: set[str] = set()
            for scope, values in (meta or {}).items():
                if not isinstance(values, dict):
                    continue
                for name, details in sorted(values.items()):
                    # Daemons expose ceph_version_short, nodes a nested
                    # version dict. Handle both rather than guessing.
                    version = "?"
                    if isinstance(details, dict):
                        raw = details.get("version")
                        if isinstance(raw, dict):
                            version = raw.get("str", "?")
                        elif raw:
                            version = str(raw)
                        else:
                            version = str(details.get("ceph_version_short") or "?")
                    else:
                        version = str(details)
                    if version != "?":
                        distinct.add(version)
                    table.add_row(scope, str(name), version)
            console.print(table)

            if len(distinct) > 1:
                print_warning(
                    "Several Ceph versions are running at once. Finish the upgrade "
                    "before any other maintenance."
                )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("log")
@fleet(parallel=True)
@async_to_sync
async def ceph_log(
    node: str = typer.Argument(None, help="Node to read the log from"),
    limit: int = typer.Option(50, "--limit", "-l", help="Number of lines"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Tail of the Ceph log."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if node is None:
                node = await _first_node(client)
            try:
                lines = await client.get_ceph_log(node, limit=limit) or []
            except PVECliError as e:
                if _absent(e):
                    _no_ceph()
                    return
                raise

            if not lines:
                print_info("Ceph log is empty")
                return

            console.print(f"[bold cyan]Ceph log on {node} (last {len(lines)} lines)[/bold cyan]")
            for entry in lines:
                text = entry.get("t", "") if isinstance(entry, dict) else str(entry)
                lowered = text.lower()
                # markup=False: log lines carry brackets of their own, such as
                # osd_mclock_max_capacity_iops_[hdd|ssd], which Rich would eat
                # as a tag, or reject outright when it looks like a closing one.
                if "err" in lowered:
                    console.print(text, style="red", markup=False, highlight=False)
                elif "warn" in lowered:
                    console.print(text, style="yellow", markup=False, highlight=False)
                else:
                    console.print(text, markup=False, highlight=False)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
