"""Node management commands."""

import re

import typer
from rich.panel import Panel
from rich.table import Table

from ..api.client import ProxmoxClient
from ..api.exceptions import PermissionError, PVECliError
from ..config import ConfigManager
from ..utils import (
    JSON_OPTION,
    build_ordered_table,
    confirm,
    console,
    emit_json,
    format_bytes,
    format_percentage,
    format_uptime,
    print_cancelled,
    print_error,
    print_info,
    print_success,
    print_warning,
)
from ..utils.fleet import fleet
from ..utils.helpers import async_to_sync, ordered_group
from ..utils.menu import select_menu
from ..utils.network import resolve_node_host
from ._shared import detect_connected_node, pick_node

app = typer.Typer(
    help="Manage cluster nodes",
    no_args_is_help=True,
    cls=ordered_group(["shutdown", "reboot", "evacuate", "maintenance", "updates", "vnc", "ssh", "list", "show"]),
)


@app.command("shutdown")
@async_to_sync
async def shutdown_node(
    node: str = typer.Argument(None, help="Node name"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Shutdown a node. Proxmox will stop all guests before powering off."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if not node:
                node = await pick_node(client)
                if node is None:
                    return

            # Check node exists and is online
            nodes = await client.get_nodes()
            node_info = next((n for n in nodes if n.get("node") == node), None)
            if not node_info:
                print_error(f"Node '{node}' not found")
                raise typer.Exit(1)
            if node_info.get("status") != "online":
                print_error(f"Node '{node}' is not online (status: {node_info.get('status')})")
                raise typer.Exit(1)

            # Detect if this is the connected node
            connected = await detect_connected_node(client, profile_config.host)
            if connected == node:
                print_warning(
                    "You are connected to this node. You will lose CLI access after shutdown."
                )

            if not yes:
                if not confirm(
                    f"Shutdown node '{node}'? All guests will be stopped and the node will power off.",
                    default=False,
                ):
                    print_cancelled()
                    return

            await client.node_command(node, "shutdown")
            print_success(
                f"Shutdown command sent to '{node}'. "
                "Proxmox will stop all guests then power off the node."
            )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("reboot")
@async_to_sync
async def reboot_node(
    node: str = typer.Argument(None, help="Node name"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Reboot a node. Proxmox will stop all guests before rebooting."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if not node:
                node = await pick_node(client)
                if node is None:
                    return

            # Check node exists and is online
            nodes = await client.get_nodes()
            node_info = next((n for n in nodes if n.get("node") == node), None)
            if not node_info:
                print_error(f"Node '{node}' not found")
                raise typer.Exit(1)
            if node_info.get("status") != "online":
                print_error(f"Node '{node}' is not online (status: {node_info.get('status')})")
                raise typer.Exit(1)

            # Detect if this is the connected node
            connected = await detect_connected_node(client, profile_config.host)
            if connected == node:
                print_warning(
                    "You are connected to this node. CLI access will be temporarily lost during reboot."
                )

            if not yes:
                if not confirm(
                    f"Reboot node '{node}'? All guests will be stopped and the node will restart.",
                    default=False,
                ):
                    print_cancelled()
                    return

            await client.node_command(node, "reboot")
            print_success(
                f"Reboot command sent to '{node}'. "
                "Proxmox will stop all guests then reboot the node."
            )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("list")
@fleet(parallel=True)
@async_to_sync
async def list_nodes(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    order: str = typer.Option(None, "--order", "-o", help="Sort by column (moved to first position), e.g. node, cpu, memory, uptime"),
    json_output: bool = JSON_OPTION,
) -> None:
    """List all cluster nodes."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)
        profile_name = profile or config_manager.get().default_profile

        async with ProxmoxClient(profile_config) as client:
            nodes = await client.get_nodes()

            if json_output:
                # Raw /nodes entries plus the percentages the table would compute:
                # bytes stay integers, "cpu" stays a 0..1 fraction of maxcpu.
                payload = []
                for node in nodes:
                    mem_used = node.get("mem") or 0
                    mem_total = node.get("maxmem") or 0
                    disk_used = node.get("disk") or 0
                    disk_total = node.get("maxdisk") or 0
                    payload.append(
                        {
                            **node,
                            "cpu_percent": (node.get("cpu") or 0.0) * 100,
                            "mem_percent": (mem_used / mem_total * 100) if mem_total else 0.0,
                            "disk_percent": (disk_used / disk_total * 100) if disk_total else 0.0,
                        }
                    )
                emit_json({"profile": profile_name, "nodes": payload})
                return

            if not nodes:
                console.print("No nodes found")
                return

            columns = [
                ("Node", {"style": "cyan"}),
                ("Status", {"style": "green"}),
                ("CPU", {"justify": "right"}),
                ("Memory", {"justify": "right"}),
                ("Disk", {"justify": "right"}),
                ("Uptime", {}),
            ]

            rows = []
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

                rows.append({
                    "Node": (node.get("node", "unknown"), node.get("node", "unknown")),
                    "Status": (status, f"[{status_color}]{status}[/{status_color}]"),
                    "CPU": (cpu_usage, f"{format_percentage(cpu_usage)} ({maxcpu} cores)"),
                    "Memory": (mem_percent, f"{format_bytes(mem_used)} / {format_bytes(mem_total)} "
                               f"({format_percentage(mem_percent)})"),
                    "Disk": (disk_percent, f"{format_bytes(disk_used)} / {format_bytes(disk_total)} "
                             f"({format_percentage(disk_percent)})"),
                    "Uptime": (uptime, uptime_str if uptime > 0 else "-"),
                })

            table = build_ordered_table("Cluster Nodes", columns, rows, order)
            if table is None:
                raise typer.Exit(1)
            console.print(table)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("info")
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
    node: str = typer.Argument(None, help="Node name(s) - single or comma-separated"),
    no_background: bool = typer.Option(False, "--no-background", "-b", is_flag=True, help="Run VNC server in foreground (blocking, single node only)"),
    split: bool = typer.Option(False, "--split", "-s", is_flag=True, help="Open each shell in a separate browser window instead of tabs"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Open authenticated VNC shell(s) for one or more nodes."""
    from ..utils import open_browser_window, print_success
    from ..utils.network import find_free_port
    from ..vnc.server import VNCProxyServer, spawn_background_server

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
                picked = await pick_node(client)
                if picked is None:
                    return
                node_list = [picked]
            else:
                node_list = [n.strip() for n in node.split(",") if n.strip()]

            if no_background and len(node_list) > 1:
                print_warning("--no-background only supports a single node, running in background")
                no_background = False

            nodes = await client.get_nodes()
            host = resolve_node_host(profile_config)

            consoles = []
            for node_name in node_list:
                node_info = next((n for n in nodes if n.get("node") == node_name), None)

                if not node_info:
                    print_error(f"Node '{node_name}' not found")
                    continue

                node_status = node_info.get("status", "unknown")
                if node_status != "online":
                    print_error(f"Node '{node_name}' is not online (status: {node_status})")
                    continue

                vnc_data = await client.create_vnc_shell(node_name, websocket=True)

                consoles.append((node_name, {
                    "proxmox_host": host,
                    "proxmox_port": profile_config.port,
                    "ws_path": f"/api2/json/nodes/{node_name}/vncwebsocket",
                    "vncticket": vnc_data["ticket"],
                    "pve_port": int(vnc_data["port"]),
                    "auth_headers": dict(client._headers),
                    "local_port": find_free_port(),
                    "verify_ssl": profile_config.verify_ssl,
                    "vnc_password": vnc_data.get("password") or vnc_data["ticket"],
                }))

        if not consoles:
            raise typer.Exit(1)

        if no_background:
            node_name, server_config = consoles[0]
            server = VNCProxyServer(**server_config)
            open_browser_window(server.get_browser_url(), new_window=split)
            print_success(f"Opening VNC shell for node '{node_name}'...")
            console.print("[dim]Press Enter to stop the server[/dim]")
            await server.run()
        else:

            for node_name, server_config in consoles:
                server = VNCProxyServer(**server_config)
                proc = spawn_background_server(server_config)
                open_browser_window(server.get_browser_url(), new_window=split)
                print_success(f"VNC shell for node '{node_name}' running in background (PID: {proc.pid})")

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


@app.command("evacuate")
@async_to_sync
async def evacuate_node(
    node: str = typer.Argument(..., help="Node to empty"),
    target: str = typer.Argument(..., help="Node receiving its guests"),
    with_local_disks: bool = typer.Option(False, "--with-local-disks", is_flag=True, help="Copy local disks during live migrations"),
    max_workers: int = typer.Option(None, "--max-workers", help="Migrations running at the same time"),
    yes: bool = typer.Option(False, "--yes", "-y", is_flag=True, help="Skip confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Move every guest off a node before maintenance.

    Same as: pvecli cluster guests migrate TARGET --node NODE

    Examples:
        pvecli node evacuate pve1 pve2
        pvecli node evacuate pve1 pve2 --with-local-disks --max-workers 2
    """
    from .cluster import _guests_command

    params = {
        "target": target,
        "max-workers": max_workers,
        "with-local-disks": 1 if with_local_disks else None,
    }
    await _guests_command(
        "migrate", None, None, None, node, yes, profile, f"Evacuate {node} to {target}",
        {**params, "online": 1}, params, exclude_node=target,
    )


# ── node updates ─────────────────────────────────────────────────────────


@app.command("updates")
@fleet(parallel=True)
@async_to_sync
async def node_updates(
    node: str = typer.Argument(None, help="Node (default: every online node)"),
    refresh: bool = typer.Option(False, "--refresh", is_flag=True, help="Run apt update on the nodes first"),
    packages: bool = typer.Option(False, "--packages", is_flag=True, help="List the packages, not only their count"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile(s): name, list, pattern ('client-*') or @group"),
    json_output: bool = JSON_OPTION,
) -> None:
    """Pending package updates per node (read only unless --refresh).

    The list comes from the last apt refresh of each node; --refresh runs
    one first (apt update, nothing is installed).
    """
    import asyncio

    config_manager = ConfigManager()
    try:
        async with ProxmoxClient(config_manager.get_profile(profile)) as client:
            online = sorted(n["node"] for n in await client.get_nodes() if n.get("status") == "online")
            nodes = [node] if node else online
            unknown = [n for n in nodes if n not in online]
            if unknown:
                raise PVECliError(f"Node(s) not online: {', '.join(unknown)}. Online: {', '.join(online)}")
            if refresh:
                upids = await asyncio.gather(*(client.post(f"/nodes/{n}/apt/update") for n in nodes))
                await asyncio.gather(*(client.wait_for_task(n, u, timeout=600) for n, u in zip(nodes, upids, strict=True)))
            lists = await asyncio.gather(*(client.get(f"/nodes/{n}/apt/update") for n in nodes))
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)

    result = {
        n: sorted(
            ({"package": p.get("Package"), "from": p.get("OldVersion"), "to": p.get("Version"),
              "origin": p.get("Origin")} for p in (lst or [])),
            key=lambda x: x["package"] or "",
        )
        for n, lst in zip(nodes, lists, strict=True)
    }
    if json_output:
        emit_json(result)
        return
    table = Table(header_style="bold cyan")
    table.add_column("Node", style="cyan")
    table.add_column("Pending", justify="right")
    table.add_column("Proxmox", justify="right")
    table.add_column("Kernel")
    for n, pkgs in result.items():
        pve = [p for p in pkgs if p["origin"] == "Proxmox"]
        # A new kernel arrives as a new package (no previous version), an
        # update of the meta package as an upgrade: either counts
        kernels = [p["to"] for p in pkgs if str(p["package"]).startswith("proxmox-kernel-")]
        kernel = max(kernels, key=lambda v: [int(x) if x.isdigit() else 0 for x in re.split(r"[.-]", v)], default=None)
        table.add_row(n, str(len(pkgs)), str(len(pve)), kernel or "-")
    console.print(table)
    if packages:
        for n, pkgs in result.items():
            if pkgs:
                console.print(f"\n[bold]{n}[/bold]")
                for p in pkgs:
                    console.print(f"  {p['package']}  {p['from'] or 'new'} -> {p['to']}  [dim]{p['origin']}[/dim]")
    if not refresh:
        print_info("From the last apt refresh of each node. Refresh with --refresh")


# ── node maintenance ─────────────────────────────────────────────────────

maintenance_app = typer.Typer(
    help="Prepare a node for manual maintenance, then bring it back",
    no_args_is_help=True,
    cls=ordered_group(["enter", "exit", "status"]),
)
app.add_typer(maintenance_app, name="maintenance")


def _state_dir():
    from pathlib import Path

    d = Path.home() / ".config" / "pvecli" / "maintenance"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_file(cluster: str, node: str):
    # Keyed by cluster, not profile: two profiles on one cluster must see
    # the same maintenance records
    return _state_dir() / f"{cluster}__{node}.json"


def _profile_name(profile: str | None) -> str:
    return profile or ConfigManager().get().default_profile or "default"


async def _cluster_key(client: ProxmoxClient) -> str:
    status = await client.get_cluster_status() or []
    cluster = next((x for x in status if x.get("type") == "cluster"), None)
    if cluster:
        return str(cluster.get("name"))
    return "standalone-" + next((x.get("name", "?") for x in status if x.get("type") == "node"), "?")


async def _missing_privileges(
    client: ProxmoxClient, guests: list[dict], ceph_flag: bool, ha_failback: bool
) -> list[str]:
    """Privileges the current user lacks for a maintenance, read from its
    effective permissions so nothing starts that cannot be finished."""
    import asyncio

    async def privs(path: str) -> dict:
        data = await client.get("/access/permissions", params={"path": path}) or {}
        return data.get(path, {})

    missing: list[str] = []
    root = await privs("/")
    if ceph_flag and not root.get("Sys.Modify"):
        missing.append("Sys.Modify on / (Ceph noout flag, or pass --no-ceph-flag)")
    if ha_failback and not root.get("Sys.Console"):
        missing.append("Sys.Console on / (HA failback)")
    per_guest = await asyncio.gather(*(privs(f"/vms/{g['vmid']}") for g in guests))
    blocked = [str(g["vmid"]) for g, p in zip(guests, per_guest, strict=True) if not p.get("VM.Migrate")]
    if blocked:
        missing.append(f"VM.Migrate on {', '.join(blocked)}")
    return missing


@maintenance_app.command("enter")
@async_to_sync
async def maintenance_enter(
    node: str = typer.Argument(..., help="Node going into maintenance"),
    target: str = typer.Option(None, "--target", "-t", help="Node receiving its guests (default: most free memory)"),
    with_local_disks: bool = typer.Option(False, "--with-local-disks", is_flag=True, help="Copy local disks during live migrations"),
    no_ceph_flag: bool = typer.Option(False, "--no-ceph-flag", is_flag=True, help="Do not set the Ceph noout flag"),
    yes: bool = typer.Option(False, "--yes", "-y", is_flag=True, help="Skip confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Empty a node and protect the cluster before working on it by hand.

    Moves its guests away, sets the Ceph noout flag (so its OSDs going down
    trigger no rebalance) and records what it did. Upgrade or reboot the
    node yourself, then run `node maintenance exit`.
    """
    import json
    import time

    from .cluster import _run_bulk

    name = _profile_name(profile)

    config_manager = ConfigManager()
    try:
        async with ProxmoxClient(config_manager.get_profile(profile)) as client:
            cluster_key = await _cluster_key(client)
            state_path = _state_file(cluster_key, node)
            if state_path.exists():
                started = json.loads(state_path.read_text()).get("started")
                raise PVECliError(f"{node} is already in maintenance (since {started})")
            status = await client.get_cluster_status() or []
            cluster = next((x for x in status if x.get("type") == "cluster"), {})
            if cluster and not cluster.get("quorate"):
                raise PVECliError("The cluster is not quorate: fix that before any maintenance")
            nodes = {n["node"]: n for n in await client.get_nodes()}
            if nodes.get(node, {}).get("status") != "online":
                raise PVECliError(f"Node '{node}' is not online")
            others = [n for n, v in nodes.items() if n != node and v.get("status") == "online"]
            if not others:
                raise PVECliError(f"No other online node to move the guests of {node} to")
            if target is None:
                target = max(others, key=lambda n: (nodes[n].get("maxmem", 0) - nodes[n].get("mem", 0)))
            elif target not in others:
                raise PVECliError(f"Target '{target}' is not another online node: {', '.join(others)}")

            guests = [
                g for g in await client.get_cluster_resources(resource_type="vm")
                if g.get("node") == node and g.get("type") in ("qemu", "lxc") and not g.get("template")
            ]
            try:
                ceph_present = bool(await client.get("/cluster/ceph/status"))
            except PVECliError:
                ceph_present = False
            noout_already = False
            if ceph_present:
                flags = await client.get("/cluster/ceph/flags") or []
                noout_already = any(f.get("name") == "noout" and f.get("value") for f in flags)
            ha_sids = {r.get("sid") for r in await client.get_ha_resources()}
            ha_here = [g for g in guests if f"{'vm' if g['type'] == 'qemu' else 'ct'}:{g['vmid']}" in ha_sids]

            console.print(f"\n[bold cyan]═══ Maintenance of {node} ═══[/bold cyan]\n")
            console.print(f"  [bold]Guests to move:[/bold] {len(guests)} to {target}")
            if ceph_present and not no_ceph_flag:
                console.print("  [bold]Ceph:[/bold]           " + ("noout already set" if noout_already else "set noout"))
            ha_mode = await client.get_ha_placement_mode() if ha_here else None
            if ha_here and ha_mode == "rules":
                console.print(
                    f"  [bold]HA:[/bold]             {len(ha_here)} guest(s), failback paused until exit"
                )
            elif ha_here:
                # Groups carry nofailback for every guest of the group: not
                # something to flip for one node's maintenance
                print_warning(
                    f"{len(ha_here)} guest(s) are under HA groups. To keep HA from moving them "
                    f"back while {node} reboots, run on it (root): "
                    f"ha-manager crm-command node-maintenance enable {node}"
                )
            missing = await _missing_privileges(
                client, guests,
                ceph_flag=ceph_present and not no_ceph_flag and not noout_already,
                ha_failback=bool(ha_here) and ha_mode == "rules",
            )
            if missing:
                raise PVECliError(
                    "This user cannot carry out the maintenance, nothing was changed. "
                    "Missing: " + "; ".join(missing)
                )
            if not yes and not confirm(f"Put {node} into maintenance?", default=False):
                print_cancelled()
                return

            state = {
                "profile": name, "cluster": cluster_key, "node": node, "target": target,
                "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                "guests": [{"vmid": g["vmid"], "type": g["type"], "status": g.get("status")} for g in guests],
                "noout_set": False,
                "failback_paused": [],
            }
            if ceph_present and not no_ceph_flag and not noout_already:
                try:
                    await client.set_ceph_flag("noout")
                except PermissionError as e:
                    # Nothing has changed yet: stop here with the way out
                    raise PVECliError(
                        f"{e}. This user cannot set Ceph flags. Nothing was changed. "
                        "Run with a user holding Sys.Modify on /, or pass --no-ceph-flag "
                        "to skip noout (Ceph then rebalances when the node's OSDs go down)"
                    )
                state["noout_set"] = True
                print_success("Ceph noout flag set")
            # HA brings a guest back to its preferred node as soon as that
            # node is up (failback), which would happen at every reboot of
            # the maintenance. Pause it on the guests leaving, through the
            # API: no root shell on the node is needed.
            if ha_here and ha_mode == "rules":
                for g in ha_here:
                    sid = f"{'vm' if g['type'] == 'qemu' else 'ct'}:{g['vmid']}"
                    try:
                        cfg = await client.get_ha_resource(sid) or {}
                        if str(cfg.get("failback", 1)) != "0":
                            await client.update_ha_resource(sid, failback=0)
                            state["failback_paused"].append(sid)
                    except PVECliError as e:
                        print_warning(f"Could not pause failback of {sid}: {e}")
                if state["failback_paused"]:
                    print_success(f"HA failback paused on {len(state['failback_paused'])} guest(s)")
            # Recorded before moving anything: an interrupted evacuation can
            # still be undone with `exit`
            state_path.write_text(json.dumps(state, indent=2))

            if guests:
                params = {"target": target, "with-local-disks": 1 if with_local_disks else None}
                try:
                    await _run_bulk(
                        client, "migrate", guests, True,
                        {**params, "online": 1}, {**params}, f"Evacuate {node} to {target}",
                    )
                except typer.Exit:
                    print_warning(
                        f"{node} is in maintenance but not empty: check the guests above before "
                        "touching it"
                    )
                    raise
            print_success(f"{node} is in maintenance")
            console.print(
                f"\n  Upgrade or reboot it, then bring it back with:\n"
                f"    pvecli node maintenance exit {node} -p {name}\n"
            )
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@maintenance_app.command("exit")
@async_to_sync
async def maintenance_exit(
    node: str = typer.Argument(..., help="Node coming back from maintenance"),
    no_return: bool = typer.Option(False, "--no-return", is_flag=True, help="Leave the guests where they are"),
    wait: int = typer.Option(600, "--wait", help="Seconds to wait for the node to be online again"),
    yes: bool = typer.Option(False, "--yes", "-y", is_flag=True, help="Skip confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Bring a node back: check the cluster, clear noout, move its guests home."""
    import asyncio
    import json
    import time

    from .cluster import _run_bulk

    config_manager = ConfigManager()
    try:
        async with ProxmoxClient(config_manager.get_profile(profile)) as client:
            cluster_key = await _cluster_key(client)
            state_path = _state_file(cluster_key, node)
            if not state_path.exists():
                raise PVECliError(f"No maintenance recorded for {node} on cluster {cluster_key}")
            state = json.loads(state_path.read_text())
            current_all = [
                g for g in await client.get_cluster_resources(resource_type="vm")
                if g.get("type") in ("qemu", "lxc")
            ]
            recorded = {g["vmid"] for g in state.get("guests", [])}
            missing = await _missing_privileges(
                client,
                [] if no_return else [g for g in current_all if g["vmid"] in recorded],
                ceph_flag=bool(state.get("noout_set")),
                ha_failback=bool(state.get("failback_paused")),
            )
            if missing:
                raise PVECliError(
                    "This user cannot bring the node back, nothing was changed. "
                    "Missing: " + "; ".join(missing)
                )
            deadline = time.time() + wait
            while True:
                nodes = {n["node"]: n for n in await client.get_nodes()}
                if nodes.get(node, {}).get("status") == "online":
                    break
                if time.time() > deadline:
                    raise PVECliError(f"{node} is still not online after {wait}s")
                print_info(f"Waiting for {node} to be online...")
                await asyncio.sleep(10)
            status = await client.get_cluster_status() or []
            cluster = next((x for x in status if x.get("type") == "cluster"), {})
            if cluster and not cluster.get("quorate"):
                raise PVECliError("The cluster is not quorate yet: wait before leaving maintenance")

            other_maint = [p for p in _state_dir().glob(f"{cluster_key}__*.json") if p != state_path]
            if state.get("noout_set"):
                if other_maint:
                    print_warning(
                        "noout stays set: other node(s) are still in maintenance: "
                        + ", ".join(p.stem.split("__", 1)[1] for p in other_maint)
                    )
                else:
                    await client.unset_ceph_flag("noout")
                    print_success("Ceph noout flag cleared")

            back = [g for g in state.get("guests", [])]
            if back and not no_return:
                current = {
                    g["vmid"]: g for g in await client.get_cluster_resources(resource_type="vm")
                    if g.get("type") in ("qemu", "lxc")
                }
                movable = [current[g["vmid"]] for g in back if g["vmid"] in current and current[g["vmid"]].get("node") != node]
                if movable:
                    if not yes and not confirm(f"Move {len(movable)} guest(s) back to {node}?", default=True):
                        print_info("Guests left where they are")
                    else:
                        await _run_bulk(
                            client, "migrate", movable, True,
                            {"target": node, "online": 1}, {"target": node}, f"Return to {node}",
                        )
            paused = state.get("failback_paused") or []
            for sid in paused:
                try:
                    await client.update_ha_resource(sid, failback=1)
                except PVECliError as e:
                    print_warning(f"Could not restore failback of {sid}: {e}")
            if paused:
                print_success(f"HA failback restored on {len(paused)} guest(s)")
            state_path.unlink()
            print_success(f"{node} is out of maintenance")
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@maintenance_app.command("status")
def maintenance_status() -> None:
    """Nodes currently in maintenance, from the records pvecli keeps."""
    import json

    records = sorted(_state_dir().glob("*.json"))
    if not records:
        print_info("No node in maintenance")
        return
    table = Table(header_style="bold cyan")
    for col in ("Cluster", "Node", "By profile", "Since", "Guests moved to", "noout set by pvecli"):
        table.add_column(col)
    for p in records:
        st = json.loads(p.read_text())
        table.add_row(st.get("cluster", p.stem.split("__")[0]), st.get("node", "?"), st.get("profile", "?"),
                      st.get("started", "?"),
                      f"{len(st.get('guests', []))} -> {st.get('target')}", "yes" if st.get("noout_set") else "no")
    console.print(table)
