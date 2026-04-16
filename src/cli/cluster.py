"""Cluster management commands."""

import asyncio

import typer
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Prompt
from rich.table import Table

from ..api.client import ProxmoxClient
from ..api.exceptions import PVECliError
from ..config import ConfigManager
from ..utils import (
    confirm,
    console,
    format_bytes,
    format_percentage,
    get_status_color,
    print_cancelled,
    print_error,
    print_info,
    print_success,
    print_warning,
)
from ..utils.helpers import async_to_sync
from ._shared import detect_connected_node

app = typer.Typer(help="Manage cluster", no_args_is_help=True)


@app.command("status")
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
            table.add_column("Version")

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
                    "Yes" if quorate else "No",
                    str(version) if version != "-" else "-",
                )

            console.print(table)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("resources")
@async_to_sync
async def cluster_resources(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    resource_type: str = typer.Option(
        None, "--type", "-t", help="Filter by type (vm, ct, node, storage)"
    ),
) -> None:
    """Show cluster resources."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            resources = await client.get_cluster_resources(resource_type)

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

            uptime_days = uptime // 86400

            table.add_row(
                node,
                f"[{status_color}]{status}[/{status_color}]",
                f"{format_percentage(cpu)} ({maxcpu})",
                f"{format_bytes(mem)} / {format_bytes(maxmem)} ({format_percentage(mem_pct)})",
                f"{uptime_days}d",
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


@app.command("tasks")
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
        if not skip_ha:
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
            try:
                await client.get_ceph_status(ceph_node)
                console.print(
                    "\n[bold cyan]── Setting Ceph maintenance flags ──[/bold cyan]"
                )
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
                ceph_flags_set = True
                print_success(f"Ceph flags set: {', '.join(_CEPH_FLAGS)}")
            except PVECliError:
                print_info("Ceph not detected, skipping flag management")

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
            # Could not detect connected node — send to last in order
            last = ordered[-1].get("node", "")
            await client.node_command(last, command)
            print_success(f"{action} command sent to '{last}'")

        # ── Post-action reminders ─────────────────────────────────────
        console.print()
        if command == "reboot":
            reminders = []
            if ceph_flags_set:
                reminders.append(
                    "Unset Ceph flags: "
                    + ", ".join(f"ceph osd unset {f}" for f in _CEPH_FLAGS)
                )
            if ha_disabled:
                reminders.append(
                    "Re-enable HA resources: " + ", ".join(ha_disabled)
                )
            if reminders:
                console.print(
                    "[bold yellow]── After reboot, remember to: ──[/bold yellow]"
                )
                for r in reminders:
                    console.print(f"  • {r}")
                console.print()

        print_success(f"Cluster {command} complete.")

    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print()
        print_warning("Interrupted!")
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
    1. Disables HA resources (prevents migration during shutdown)
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
    1. Disables HA resources (prevents migration during reboot)
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
