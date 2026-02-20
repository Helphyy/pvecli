"""Node management commands."""

import typer
from rich.panel import Panel
from rich.table import Table

from ..api.client import ProxmoxClient
from ..api.exceptions import PVECliError
from ..config import ConfigManager
from ..utils import console, format_bytes, format_percentage, format_uptime, print_cancelled, print_error, print_info
from ..utils.helpers import async_to_sync, ordered_group
from ..utils.menu import select_menu
from ..utils.network import resolve_node_host
from ._shared import pick_node

app = typer.Typer(help="Manage cluster nodes", no_args_is_help=True, cls=ordered_group(["vnc", "ssh", "list", "show"]))


@app.command("list")
@async_to_sync
async def list_nodes(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """List all cluster nodes."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            nodes = await client.get_nodes()

            if not nodes:
                console.print("No nodes found")
                return

            table = Table(title="Cluster Nodes", show_header=True, header_style="bold cyan")
            table.add_column("Node", style="cyan")
            table.add_column("Status", style="green")
            table.add_column("CPU", justify="right")
            table.add_column("Memory", justify="right")
            table.add_column("Disk", justify="right")
            table.add_column("Uptime")

            for node in nodes:
                status = node.get("status", "unknown")
                status_color = "green" if status == "online" else "red"

                cpu_usage = node.get("cpu", 0) * 100
                maxcpu = node.get("maxcpu", 1)

                mem_used = node.get("mem", 0)
                mem_total = node.get("maxmem", 1)
                mem_percent = (mem_used / mem_total) * 100 if mem_total else 0

                disk_used = node.get("disk", 0)
                disk_total = node.get("maxdisk", 1)
                disk_percent = (disk_used / disk_total) * 100 if disk_total else 0

                uptime = node.get("uptime", 0)
                uptime_str = f"{uptime // 86400}d {(uptime % 86400) // 3600}h"

                table.add_row(
                    node.get("node", "unknown"),
                    f"[{status_color}]{status}[/{status_color}]",
                    f"{format_percentage(cpu_usage)} ({maxcpu} cores)",
                    f"{format_bytes(mem_used)} / {format_bytes(mem_total)} "
                    f"({format_percentage(mem_percent)})",
                    f"{format_bytes(disk_used)} / {format_bytes(disk_total)} "
                    f"({format_percentage(disk_percent)})",
                    uptime_str if uptime > 0 else "-",
                )

            console.print(table)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("show")
@async_to_sync
async def show_node(
    node: str = typer.Argument(None, help="Node name"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    all_nodes: bool = typer.Option(False, "--all", "-a", is_flag=True, help="Show all nodes"),
) -> None:
    """Show detailed information about a node."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            version = await client.get_version()
            nodes = await client.get_nodes()
            if not nodes:
                print_info("No nodes found")
                return

            if all_nodes:
                for node_info in sorted(nodes, key=lambda n: n.get("node", "")):
                    node_name = node_info.get("node", "unknown")
                    ns = node_info.get("status", "unknown")
                    status = await client.get_node_status(node_name)
                    console.print(_render_node_panel(node_name, status, version, ns))
            else:
                if not node:
                    node = await pick_node(client)
                    if node is None:
                        return

                ns = next((n.get("status", "unknown") for n in nodes if n.get("node") == node), "unknown")
                status = await client.get_node_status(node)
                console.print(_render_node_panel(node, status, version, ns))

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


def _render_node_panel(node: str, status: dict, version: dict, node_status: str = "unknown") -> Panel:
    """Build a Rich Panel for a single node."""
    lines = []

    # General
    lines.append("[bold]── General ──[/bold]")
    status_str = f"[green]{node_status}[/green]" if node_status == "online" else f"[red]{node_status}[/red]"
    lines.append(f"[bold]Status:[/bold]     {status_str}")
    lines.append(f"[bold]Uptime:[/bold]     {format_uptime(status.get('uptime', 0))}")
    lines.append(f"[bold]PVE:[/bold]        {version.get('version', '?')} (release {version.get('release', '?')})")

    # CPU
    cpu_info = status.get('cpuinfo', {})
    cpu_percent = status.get('cpu', 0) * 100
    cpu_cores = cpu_info.get('cpus', status.get('maxcpu', 0))
    cpu_model = cpu_info.get('model', 'unknown')
    lines.append("")
    lines.append("[bold]── CPU ──[/bold]")
    lines.append(f"[bold]Model:[/bold]      {cpu_model}")
    lines.append(f"[bold]Cores:[/bold]      {cpu_cores}")
    lines.append(f"[bold]Usage:[/bold]      {format_percentage(cpu_percent)}")

    # Memory
    mem_used = status.get("memory", {}).get("used", 0)
    mem_total = status.get("memory", {}).get("total", 1)
    mem_pct = (mem_used / mem_total * 100) if mem_total > 0 else 0
    lines.append("")
    lines.append("[bold]── Memory ──[/bold]")
    lines.append(f"[bold]Total:[/bold]      {format_bytes(mem_total)}")
    lines.append(f"[bold]Used:[/bold]       {format_bytes(mem_used)} ({format_percentage(mem_pct)})")
    lines.append(f"[bold]Free:[/bold]       {format_bytes(mem_total - mem_used)}")

    # Root filesystem
    disk_used = status.get("rootfs", {}).get("used", 0)
    disk_total = status.get("rootfs", {}).get("total", 1)
    disk_pct = (disk_used / disk_total * 100) if disk_total > 0 else 0
    lines.append("")
    lines.append("[bold]── Root FS ──[/bold]")
    lines.append(f"[bold]Total:[/bold]      {format_bytes(disk_total)}")
    lines.append(f"[bold]Used:[/bold]       {format_bytes(disk_used)} ({format_percentage(disk_pct)})")
    lines.append(f"[bold]Free:[/bold]       {format_bytes(disk_total - disk_used)}")

    # Swap
    swap = status.get("swap")
    if isinstance(swap, dict) and swap.get("total"):
        swap_used = swap.get("used", 0)
        swap_total = swap["total"]
        swap_pct = (swap_used / swap_total * 100) if swap_total > 0 else 0
        lines.append("")
        lines.append("[bold]── Swap ──[/bold]")
        lines.append(f"[bold]Total:[/bold]      {format_bytes(swap_total)}")
        lines.append(f"[bold]Used:[/bold]       {format_bytes(swap_used)} ({format_percentage(swap_pct)})")
        lines.append(f"[bold]Free:[/bold]       {format_bytes(swap_total - swap_used)}")

    return Panel("\n".join(lines), title=f"Node: {node}", border_style="blue")



@app.command("vnc")
@async_to_sync
async def node_vnc(
    node: str = typer.Argument(None, help="Node name"),
    background: bool = typer.Option(False, "--background", "-b", is_flag=True, help="Run VNC server in background"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Open an authenticated VNC shell for a node."""
    from ..utils import open_browser_window, print_success
    from ..utils.network import find_free_port
    from ..vnc.server import VNCProxyServer

    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        if profile_config.auth.type == "token":
            print_error(
                "Node VNC shell is not supported with API token authentication. "
                "Use a profile with password authentication instead."
            )
            raise typer.Exit(1)

        async with ProxmoxClient(profile_config) as client:
            if not node:
                node = await pick_node(client)
                if node is None:
                    return

            nodes = await client.get_nodes()
            node_info = next((n for n in nodes if n.get("node") == node), None)

            if not node_info:
                print_error(f"Node '{node}' not found")
                raise typer.Exit(1)

            node_status = node_info.get("status", "unknown")
            if node_status != "online":
                print_error(f"Node '{node}' is not online (status: {node_status})")
                raise typer.Exit(1)

            vnc_data = await client.create_vnc_shell(node, websocket=True)

            host = resolve_node_host(profile_config)

            server_config = {
                "proxmox_host": host,
                "proxmox_port": profile_config.port,
                "ws_path": f"/api2/json/nodes/{node}/vncwebsocket",
                "vncticket": vnc_data["ticket"],
                "pve_port": int(vnc_data["port"]),
                "auth_headers": dict(client._headers),
                "local_port": find_free_port(),
                "verify_ssl": profile_config.verify_ssl,
                "vnc_password": vnc_data["ticket"],
            }

        server = VNCProxyServer(**server_config)
        url = server.get_browser_url()
        open_browser_window(url)

        if background:
            import json
            import subprocess
            import sys

            proc = subprocess.Popen(
                [sys.executable, "-m", "src.vnc", json.dumps(server_config)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            print_success(f"VNC shell for node '{node}' running in background (PID: {proc.pid})")
        else:
            print_success(f"Opening VNC shell for node '{node}'...")
            console.print("[dim]Press Enter to stop the server[/dim]")
            await server.run()

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("ssh")
@async_to_sync
async def node_ssh(
    node: str = typer.Argument(None, help="Node name"),
    user: str = typer.Option(None, "--user", "-u", help="SSH user"),
    port: int = typer.Option(None, "--port", "-P", help="SSH port"),
    key: str = typer.Option(None, "--key", "-i", help="Path to SSH key"),
    command: str = typer.Option(None, "--command", "-c", help="Execute command instead of shell"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """SSH into a Proxmox node."""
    from ..ssh import build_ssh_command, exec_ssh

    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        if not node:
            async with ProxmoxClient(profile_config) as client:
                node = await pick_node(client)
                if node is None:
                    return

        host = resolve_node_host(profile_config)
        ssh_user = user or profile_config.ssh_user or "root"
        ssh_port = port or profile_config.ssh_port
        ssh_key = key or profile_config.ssh_key

        args = build_ssh_command(host, ssh_user, ssh_port, ssh_key, command=command)
        console.print(f"[dim]Connecting to {ssh_user}@{host}...[/dim]")
        exec_ssh(args)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


