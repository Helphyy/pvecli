"""Cluster management commands."""

import typer
from rich.table import Table

from ..api.client import ProxmoxClient
from ..api.exceptions import PVECliError
from ..config import ConfigManager
from ..utils import (
    console,
    format_bytes,
    format_percentage,
    get_status_color,
    print_error,
    print_info,
)
from ..utils.helpers import async_to_sync

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
