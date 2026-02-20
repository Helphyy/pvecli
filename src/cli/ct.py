"""Container (LXC) management commands."""

import asyncio
import re
from typing import Any

import typer
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from ..api.client import ProxmoxClient
from ..api.exceptions import PVECliError
from ..config import ConfigManager
from ..utils import (
    confirm,
    console,
    create_table,
    format_bytes,
    format_percentage,
    format_tags_colored,
    format_uptime,
    get_status_color,
    multi_select_menu,
    print_cancelled,
    print_error,
    print_info,
    prompt,
    print_success,
    print_warning,
    select_menu,
    usage_bar,
)
from ..utils.helpers import async_to_sync, ordered_group
from ..utils.network import resolve_node_host
from .tag import _parse_color_map
from ._shared import (
    build_kv,
    confirm_action,
    extract_size,
    parse_id_list,
    parse_kv,
    run_with_spinner,
    shared_add_tag,
    shared_create_snapshot,
    shared_delete_snapshot,
    shared_list_snapshots,
    shared_list_tags,
    shared_remove_tag,
    shared_rollback_snapshot,
    validate_resources,
)

_CMD_ORDER = [
    "start", "stop", "shutdown", "reboot",
    "add", "clone", "edit", "remove",
    "tag", "snapshot", "template",
    "vnc", "ssh",
    "list", "show",
]


app = typer.Typer(help="Manage containers (LXC)", no_args_is_help=True, cls=ordered_group(_CMD_ORDER))


@app.command("list")
@async_to_sync
async def list_containers(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    node: str = typer.Option(None, "--node", "-n", help="Filter by node"),
    status: str = typer.Option(None, "--status", "-s", help="Filter by status (running, stopped)"),
) -> None:
    """List all containers."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            containers = await client.get_containers(node=node)

            if not containers:
                print_info("No containers found")
                return

            # Filter by status if specified
            if status:
                containers = [
                    ct for ct in containers if ct.get("status", "").lower() == status.lower()
                ]

            if not containers:
                print_info(f"No containers found with status '{status}'")
                return

            # Get tag color map

            cluster_opts = await client.get_cluster_options()
            color_map = _parse_color_map(cluster_opts.get("tag-style", ""))

            # Sort by ctid
            containers = sorted(containers, key=lambda x: x.get("vmid", 0))

            table = Table(title="Containers (LXC)", show_header=True, header_style="bold cyan")
            table.add_column("CTID", style="cyan", justify="right")
            table.add_column("Name")
            table.add_column("Node")
            table.add_column("Status")
            table.add_column("CPU")
            table.add_column("Memory")
            table.add_column("Disk")
            table.add_column("Uptime")
            table.add_column("Tags")

            for ct in containers:
                ctid = str(ct.get("vmid", "-"))
                name = ct.get("name", "-")
                tags = ct.get("tags", "")
                node_name = ct.get("node", "-")
                ct_status = ct.get("status", "unknown")
                status_color = get_status_color(ct_status)

                if ct_status == "running":
                    cpu_usage = ct.get("cpu", 0) * 100
                    maxcpu = ct.get("maxcpu", ct.get("cpus", 1))
                    cpu_str = usage_bar(cpu_usage, label=f"({maxcpu}c)")

                    mem = ct.get("mem", 0)
                    maxmem = ct.get("maxmem", 1)
                    mem_percent = (mem / maxmem * 100) if maxmem else 0
                    mem_str = usage_bar(mem_percent, label=format_bytes(maxmem))

                    disk = ct.get("disk", 0)
                    maxdisk = ct.get("maxdisk", 1)
                    disk_percent = (disk / maxdisk * 100) if maxdisk else 0
                    disk_str = usage_bar(disk_percent, label=format_bytes(maxdisk))

                    uptime = ct.get("uptime", 0)
                    uptime_str = format_uptime(uptime) if uptime else "-"
                else:
                    maxcpu = ct.get("maxcpu", ct.get("cpus", 0))
                    cpu_str = f"[dim]- ({maxcpu}c)[/dim]" if maxcpu else "-"
                    maxmem = ct.get("maxmem", 0)
                    mem_str = f"[dim]- {format_bytes(maxmem)}[/dim]" if maxmem else "-"
                    maxdisk = ct.get("maxdisk", 0)
                    disk_str = f"[dim]- {format_bytes(maxdisk)}[/dim]" if maxdisk else "-"
                    uptime_str = "-"

                table.add_row(
                    ctid,
                    name,
                    node_name,
                    f"[{status_color}]{ct_status}[/{status_color}]",
                    cpu_str,
                    mem_str,
                    disk_str,
                    uptime_str,
                    format_tags_colored(tags, color_map),
                )

            console.print(table)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("show")
@async_to_sync
async def show_container(
    ctid: int = typer.Argument(None, help="Container ID (CTID)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Show detailed information about a container."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if ctid is None:
                ctid = await _select_ct(client)
                if ctid is None:
                    print_cancelled()
                    return
            # Find which node the container is on
            node, _ = await _get_container_node(client, ctid)

            # Get detailed status and config
            status = await client.get_container_status(node, vmid=ctid)
            config = await client.get_container_config(node, vmid=ctid)

            # Build the display
            ct_name = config.get("hostname", status.get("name", f"CT {ctid}"))
            ct_status = status.get("status", "unknown")
            status_color = get_status_color(ct_status)

            lines = []
            lines.append("[bold]── General ──[/bold]")
            lines.append(f"[bold]Status:[/bold]      [{status_color}]{ct_status}[/{status_color}]")
            lines.append(f"[bold]Node:[/bold]        {node}")

            if ct_status == "running":
                uptime = status.get("uptime", 0)
                if uptime:
                    lines.append(f"[bold]Uptime:[/bold]      {format_uptime(uptime)}")

            lines.append("")
            lines.append("[bold]── Resources ──[/bold]")

            # CPU
            cpus = status.get("cpus", config.get("cores", 1))
            if ct_status == "running":
                cpu_usage = status.get("cpu", 0) * 100
                lines.append(
                    f"[bold]CPU:[/bold]         {cpus} cores ({format_percentage(cpu_usage)} used)"
                )
            else:
                lines.append(f"[bold]CPU:[/bold]         {cpus} cores")

            # Memory
            maxmem = status.get("maxmem", config.get("memory", 0) * 1024 * 1024)
            if ct_status == "running":
                mem = status.get("mem", 0)
                mem_percent = (mem / maxmem * 100) if maxmem else 0
                lines.append(
                    f"[bold]Memory:[/bold]      {format_bytes(mem)} / {format_bytes(maxmem)} "
                    f"({format_percentage(mem_percent)})"
                )
            else:
                lines.append(f"[bold]Memory:[/bold]      {format_bytes(maxmem)}")

            # Swap
            maxswap = config.get("swap", 0) * 1024 * 1024
            if maxswap and ct_status == "running":
                swap = status.get("swap", 0)
                swap_percent = (swap / maxswap * 100) if maxswap else 0
                lines.append(
                    f"[bold]Swap:[/bold]        {format_bytes(swap)} / {format_bytes(maxswap)} "
                    f"({format_percentage(swap_percent)})"
                )

            # Disk
            maxdisk = status.get("maxdisk", 0)
            if maxdisk and ct_status == "running":
                disk = status.get("disk", 0)
                disk_percent = (disk / maxdisk * 100) if maxdisk else 0
                lines.append(
                    f"[bold]Disk:[/bold]        {format_bytes(disk)} / {format_bytes(maxdisk)} "
                    f"({format_percentage(disk_percent)})"
                )

            # Configuration details
            lines.append("")
            lines.append("[bold]── Configuration ──[/bold]")

            if config.get("ostype"):
                lines.append(f"[bold]OS Type:[/bold]     {config.get('ostype')}")

            if config.get("arch"):
                lines.append(f"[bold]Arch:[/bold]        {config.get('arch')}")

            # Network section - get IPs from API and show network devices
            net_devices = [k for k in config.keys() if k.startswith("net")]
            has_network_info = False

            # Get network interfaces from API
            try:
                interfaces = await client.get_container_interfaces(node, vmid=ctid)
                if interfaces:
                    has_network_info = True
                    lines.append("")
                    lines.append("[bold]── Network ──[/bold]")
                    for iface in interfaces:
                        iface_name = iface.get("name", "unknown")
                        lines.append(f"[bold]{iface_name}:[/bold]")

                        # Show IPv4
                        if iface.get("inet"):
                            lines.append(f"  IPv4: {iface.get('inet')}")

                        # Show IPv6
                        if iface.get("inet6"):
                            lines.append(f"  IPv6: {iface.get('inet6')}")

                        # Show MAC address
                        if iface.get("hwaddr"):
                            lines.append(f"  MAC:  {iface.get('hwaddr')}")
            except Exception:
                # If API call fails, fall back to parsing config
                pass

            # Show network device configuration from Proxmox config
            if net_devices:
                if not has_network_info:
                    lines.append("")
                    lines.append("[bold]── Network ──[/bold]")
                else:
                    lines.append("")
                for net_dev in sorted(net_devices):
                    lines.append(f"[bold]{net_dev}:[/bold] {config.get(net_dev)}")

            # Others section
            lines.append("")
            lines.append("[bold]── Others ──[/bold]")

            # Pool
            pool = config.get("pool", "")
            lines.append(f"[bold]Pool:[/bold]        {pool if pool else 'None'}")

            # Tags
            if config.get("tags"):
                cluster_opts = await client.get_cluster_options()
                color_map = _parse_color_map(cluster_opts.get("tag-style", ""))
                lines.append(f"[bold]Tags:[/bold]        {format_tags_colored(config.get('tags', ''), color_map)}")

            panel = Panel(
                "\n".join(lines),
                title=f"Container {ctid}: {ct_name}",
                border_style="blue",
            )
            console.print(panel)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


_CT_DISK_RE = re.compile(r"^(rootfs|mp\d+)$")
_CT_NET_RE = re.compile(r"^net\d+$")


async def _edit_ct_disks(config, changes, resizes, deletes, client, node):
    """Disk sub-menu for CT edit."""


    while True:
        disk_keys = sorted(
            k for k in set(list(config) + list(changes))
            if _CT_DISK_RE.match(k) and k not in deletes
        )

        options = []
        for dk in disk_keys:
            val = changes.get(dk, config.get(dk, ""))
            prefix = "* " if dk in changes or dk in resizes else "  "
            size_info = f" -> {resizes[dk]}" if dk in resizes else ""
            options.append(f"{prefix}{dk.ljust(10)} {str(val)[:50]}{size_info}")

        for dk in sorted(k for k in deletes if _CT_DISK_RE.match(k)):
            options.append(f"  {dk.ljust(10)} [removed]")

        options.append("  " + "─" * 50)
        options.append("  Add mountpoint")
        # Only non-rootfs mountpoints can be removed
        removable_disks = [dk for dk in disk_keys if dk != "rootfs"]
        if removable_disks:
            options.append("  Remove mountpoint")
        options.append("  Back")

        idx = select_menu(options, "\n  Disks:")

        if idx is None or options[idx].strip() == "Back":
            return

        if options[idx].strip() == "Add mountpoint":
            all_keys = set(list(config) + list(changes))
            next_i = 0
            while f"mp{next_i}" in all_keys:
                next_i += 1
            mp_name = f"mp{next_i}"

            storages = await client.get_storage_list(node)
            storage_names = [s.get("storage", "") for s in storages]
            if not storage_names:
                print_error("No storage available")
                continue

            st_idx = select_menu(storage_names, "  Storage:")
            if st_idx is None:
                continue
            storage = storage_names[st_idx]

            size = IntPrompt.ask("  Size (GB)", default=8)
            mount_path = Prompt.ask("  Mount path", default=f"/mnt/{mp_name}")

            changes[mp_name] = f"{storage}:{size},mp={mount_path}"
            continue

        if options[idx].strip() == "Remove mountpoint":
            if not removable_disks:
                continue
            rm_idx = select_menu(removable_disks + ["Cancel"], "  Remove mountpoint:")
            if rm_idx is not None and rm_idx < len(removable_disks):
                dk = removable_disks[rm_idx]
                changes.pop(dk, None)
                resizes.pop(dk, None)
                if dk in config:
                    deletes.add(dk)
            continue

        # Selected a disk -> resize
        if idx < len(disk_keys):
            dk = disk_keys[idx]
            val = str(changes.get(dk, config.get(dk, "")))
            current_size = resizes.get(dk, extract_size(val))

            console.print(f"\n  Current size: {current_size}")
            new_size = Prompt.ask("  New size in GB (empty to cancel)", default="")
            if new_size:
                try:
                    int(new_size)
                    resizes[dk] = f"{new_size}G"
                except ValueError:
                    print_error("Invalid number")


async def _edit_ct_network(config, changes, deletes, client, node):
    """Network sub-menu for CT edit."""


    while True:
        net_keys = sorted(
            k for k in set(list(config) + list(changes))
            if _CT_NET_RE.match(k) and k not in deletes
        )

        options = []
        for nk in net_keys:
            val = changes.get(nk, config.get(nk, ""))
            prefix = "* " if nk in changes else "  "
            options.append(f"{prefix}{nk.ljust(6)} {str(val)[:55]}")

        for nk in sorted(k for k in deletes if _CT_NET_RE.match(k)):
            options.append(f"  {nk.ljust(6)} [removed]")

        options.append("  " + "─" * 50)
        options.append("  Add NIC")
        if net_keys:
            options.append("  Remove NIC")
        options.append("  Back")

        idx = select_menu(options, "\n  Network:")

        if idx is None or options[idx].strip() == "Back":
            return

        if options[idx].strip() == "Add NIC":
            interfaces = await client.get_network_interfaces(node)
            bridges = [i.get("iface", "") for i in interfaces if i.get("type") == "bridge"]
            if not bridges:
                print_error("No bridges available")
                continue

            br_idx = select_menu(bridges, "  Bridge:")
            if br_idx is None:
                continue

            all_keys = set(list(config) + list(changes))
            next_i = 0
            while f"net{next_i}" in all_keys:
                next_i += 1
            iface_name = f"eth{next_i}"

            net_config = f"name={iface_name},bridge={bridges[br_idx]}"

            # IPv4
            ip_opts = ["dhcp", "static", "none"]
            ip_idx = select_menu(ip_opts, "  IPv4:")
            if ip_idx == 0:
                net_config += ",ip=dhcp"
            elif ip_idx == 1:
                ip_addr = Prompt.ask("  IPv4 CIDR (e.g. 10.0.0.5/24)")
                if ip_addr:
                    net_config += f",ip={ip_addr}"
                    gw = Prompt.ask("  Gateway (empty for none)", default="")
                    if gw:
                        net_config += f",gw={gw}"

            vlan = Prompt.ask("  VLAN tag (empty for none)", default="")
            if vlan:
                net_config += f",tag={vlan}"

            if select_menu(["No", "Yes"], "  Firewall:") == 1:
                net_config += ",firewall=1"

            changes[f"net{next_i}"] = net_config
            continue

        if options[idx].strip() == "Remove NIC":
            removable = list(net_keys)
            if not removable:
                continue
            rm_idx = select_menu(removable + ["Cancel"], "  Remove NIC:")
            if rm_idx is not None and rm_idx < len(removable):
                nk = removable[rm_idx]
                changes.pop(nk, None)
                if nk in config:
                    deletes.add(nk)
            continue

        # Edit existing NIC
        if idx < len(net_keys):
            nk = net_keys[idx]
            current_val = str(changes.get(nk, config.get(nk, "")))
            params = parse_kv(current_val)

            interfaces = await client.get_network_interfaces(node)
            bridges = [i.get("iface", "") for i in interfaces if i.get("type") == "bridge"]

            if bridges:
                current_bridge = params.get("bridge", "")
                br_idx = select_menu(bridges, f"  Bridge (current: {current_bridge}):")
                if br_idx is not None:
                    params["bridge"] = bridges[br_idx]

            # IPv4
            current_ip = params.get("ip", "")
            ip_opts = ["dhcp", "static", "none", f"keep ({current_ip})"]
            ip_idx = select_menu(ip_opts, f"  IPv4 (current: {current_ip or 'none'}):")
            if ip_idx == 0:
                params["ip"] = "dhcp"
                params.pop("gw", None)
            elif ip_idx == 1:
                ip_addr = Prompt.ask("  IPv4 CIDR", default=current_ip if current_ip and current_ip != "dhcp" else "")
                if ip_addr:
                    params["ip"] = ip_addr
                    current_gw = params.get("gw", "")
                    gw = Prompt.ask("  Gateway", default=current_gw if current_gw else "")
                    if gw:
                        params["gw"] = gw
                    elif "gw" in params:
                        del params["gw"]
            elif ip_idx == 2:
                params.pop("ip", None)
                params.pop("gw", None)

            # VLAN
            current_vlan = params.get("tag", "")
            new_vlan = Prompt.ask("  VLAN tag", default=current_vlan if current_vlan else "")
            if new_vlan:
                params["tag"] = new_vlan
            elif "tag" in params:
                del params["tag"]

            # Firewall
            current_fw = params.get("firewall", "0") == "1"
            fw_idx = select_menu(["No", "Yes"], f"  Firewall (current: {'Yes' if current_fw else 'No'}):")
            if fw_idx is not None:
                if fw_idx == 1:
                    params["firewall"] = "1"
                elif "firewall" in params:
                    del params["firewall"]

            new_val = build_kv(params)
            if new_val != current_val:
                changes[nk] = new_val
            elif nk in changes:
                del changes[nk]


@app.command("edit")
@async_to_sync
async def edit_container(
    ctid: int = typer.Argument(None, help="Container ID (CTID)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Interactively edit container configuration."""


    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if ctid is None:
                ctid = await _select_ct(client)
                if ctid is None:
                    print_cancelled()
                    return
            node, _ = await _get_container_node(client, ctid)

            config = await client.get_container_config(node, vmid=ctid)

            # Get current pool from cluster resources
            resources = await client.get_cluster_resources(resource_type="vm")
            ct_resource = next(
                (r for r in resources if r.get("vmid") == ctid and r.get("type") == "lxc"), None
            )
            current_pool = ct_resource.get("pool", "") if ct_resource else ""

            # Simple fields: (api_key, label, type, default)
            fields = [
                ("hostname", "Hostname", str, ""),
                ("cores", "CPU Cores", int, 1),
                ("memory", "Memory (MB)", int, 512),
                ("swap", "Swap (MB)", int, 512),
                ("onboot", "Start on boot", bool, False),
                ("nameserver", "Nameserver", str, ""),
                ("searchdomain", "Search domain", str, ""),
                ("description", "Description", str, ""),
            ]

            changes: dict = {}
            resizes: dict = {}
            deletes: set = set()
            pool_change: tuple | None = None
            max_label = max(len(f[1]) for f in fields)

            while True:
                options = []

                # Simple fields
                pw_menu_idx = -1
                for key, label, ftype, default in fields:
                    raw = config.get(key, default)
                    current = changes.get(key, bool(raw) if ftype is bool else raw)
                    if ftype is bool:
                        display = "Yes" if current else "No"
                    else:
                        s = str(current)
                        display = s[:50] + "..." if len(s) > 50 else s
                    prefix = "* " if key in changes else "  "
                    options.append(f"{prefix}{label.ljust(max_label)}  {display}")

                    # Password right after Hostname
                    if key == "hostname":
                        pw_prefix = "* " if "password" in changes else "  "
                        pw_display = "(set)" if "password" in changes else "(unchanged)"
                        options.append(f"{pw_prefix}{'Password'.ljust(max_label)}  {pw_display}")
                        pw_menu_idx = len(options) - 1

                # Pool
                pool_display = pool_change[1] if pool_change else current_pool
                pool_prefix = "* " if pool_change else "  "
                options.append(f"{pool_prefix}{'Pool'.ljust(max_label)}  {pool_display or '(none)'}")
                pool_menu_idx = len(options) - 1

                # Tags
                orig_tags = config.get("tags", "")
                current_tags_str = changes.get("tags", orig_tags)
                tags_prefix = "* " if "tags" in changes else "  "
                tags_display = current_tags_str if current_tags_str else "(none)"
                options.append(f"{tags_prefix}{'Tags'.ljust(max_label)}  {tags_display}")
                tags_menu_idx = len(options) - 1

                # Separator + sub-menus
                options.append("  " + "─" * (max_label + 20))

                disk_keys = sorted(
                    k for k in set(list(config) + list(changes))
                    if _CT_DISK_RE.match(k) and k not in deletes
                )
                disk_mod = len(resizes) + len([k for k in changes if _CT_DISK_RE.match(k)]) + len([k for k in deletes if _CT_DISK_RE.match(k)])
                disk_label = f"Disks         [{', '.join(disk_keys)}]" if disk_keys else "Disks         (none)"
                options.append(f"{'* ' if disk_mod else '  '}{disk_label}")
                disks_menu_idx = len(options) - 1

                net_keys = sorted(
                    k for k in set(list(config) + list(changes))
                    if _CT_NET_RE.match(k) and k not in deletes
                )
                net_mod = len([k for k in changes if _CT_NET_RE.match(k)]) + len([k for k in deletes if _CT_NET_RE.match(k)])
                net_label = f"Network       [{', '.join(net_keys)}]" if net_keys else "Network       (none)"
                options.append(f"{'* ' if net_mod else '  '}{net_label}")
                net_menu_idx = len(options) - 1

                # Apply / Cancel
                options.append("  " + "─" * (max_label + 20))
                total = len(changes) + len(resizes) + len(deletes) + (1 if pool_change else 0)
                options.append(f"  Apply {total} change(s)" if total else "  (no changes)")
                options.append("  Cancel")

                selected = select_menu(options, f"\n  CT {ctid}: {config.get('hostname', '')}")

                if selected is None or selected == len(options) - 1:
                    print_cancelled()
                    return

                if selected == len(options) - 2 and total:
                    break

                if selected == pool_menu_idx:
                    pools = await client.get_pools()
                    pool_names = ["(none)"] + [p.get("poolid", "") for p in pools]
                    pi = select_menu(pool_names, "  Pool:")
                    if pi is not None:
                        new_pool = "" if pi == 0 else pool_names[pi]
                        pool_change = (current_pool, new_pool) if new_pool != current_pool else None
                    continue

                if selected == tags_menu_idx:
                    # Collect all known tags from cluster
                    all_resources = await client.get_cluster_resources(resource_type="vm")
                    known_tags = set()
                    for r in all_resources:
                        for t in r.get("tags", "").split(";"):
                            t = t.strip()
                            if t:
                                known_tags.add(t)
                    # Also add tags from color-map
                    cluster_opts = await client.get_cluster_options()
                    cm = _parse_color_map(cluster_opts.get("tag-style", ""))
                    known_tags.update(cm)

                    current_tags = [t.strip() for t in current_tags_str.split(";") if t.strip()]
                    tag_list = sorted(known_tags)
                    preselected = [i for i, t in enumerate(tag_list) if t in current_tags]

                    entries = tag_list + ["+ Add custom tag"]
                    sel = multi_select_menu(entries, "  Tags (Space to toggle, Enter to confirm):", preselected=preselected)
                    if sel is not None:
                        chosen = [entries[i] for i in sel]
                        # Handle custom tag
                        result_tags = [t for t in chosen if t != "+ Add custom tag"]
                        if "+ Add custom tag" in chosen:
                            custom = Prompt.ask("  Custom tag name")
                            if custom and custom.strip():
                                result_tags.append(custom.strip())
                        new_tags = ";".join(sorted(result_tags))
                        if new_tags != orig_tags:
                            changes["tags"] = new_tags
                        elif "tags" in changes:
                            del changes["tags"]
                    continue

                if selected == pw_menu_idx:
                    import getpass
                    pw = getpass.getpass("  New password: ")
                    if not pw:
                        changes.pop("password", None)
                        continue
                    if len(pw) < 5:
                        print_error("Password must be at least 5 characters")
                        continue
                    pw_confirm = getpass.getpass("  Confirm password: ")
                    if pw == pw_confirm:
                        changes["password"] = pw
                    else:
                        print_error("Passwords do not match")
                    continue

                if selected == disks_menu_idx:
                    await _edit_ct_disks(config, changes, resizes, deletes, client, node)
                    continue

                if selected == net_menu_idx:
                    await _edit_ct_network(config, changes, deletes, client, node)
                    continue

                # Simple field edit (Password is at index 1, shifting fields after hostname)
                field_idx = selected if selected < 1 else selected - 1
                if selected != pw_menu_idx and field_idx < len(fields):
                    key, label, ftype, default = fields[field_idx]
                    raw = config.get(key, default)
                    original = bool(raw) if ftype is bool else raw
                    current = changes.get(key, original)

                    if ftype is bool:
                        si = select_menu(["Yes", "No"], f"  {label}:")
                        if si is not None:
                            new_val = si == 0
                            if new_val != original:
                                changes[key] = new_val
                            elif key in changes:
                                del changes[key]
                    elif ftype is int:
                        raw_input = Prompt.ask(f"  {label}", default=str(current))
                        try:
                            new_val = int(raw_input)
                            if new_val != original:
                                changes[key] = new_val
                            elif key in changes:
                                del changes[key]
                        except ValueError:
                            print_error("Invalid number")
                    else:
                        new_val = Prompt.ask(f"  {label}", default=str(current) if current else "")
                        if new_val != str(original):
                            changes[key] = new_val
                        elif key in changes:
                            del changes[key]

            # Summary
            console.print("\n[bold]Changes:[/bold]")

            for key, label, ftype, default in fields:
                if key in changes:
                    raw = config.get(key, default)
                    if ftype is bool:
                        console.print(f"  {label}: {'Yes' if raw else 'No'} -> {'Yes' if changes[key] else 'No'}")
                    else:
                        console.print(f"  {label}: {raw} -> {changes[key]}")

            if "password" in changes:
                console.print("  Password: (will be changed)")

            if pool_change:
                console.print(f"  Pool: {pool_change[0] or '(none)'} -> {pool_change[1] or '(none)'}")

            if "tags" in changes:
                console.print(f"  Tags: {config.get('tags', '') or '(none)'} -> {changes['tags'] or '(none)'}")

            for dk in sorted(k for k in changes if _CT_DISK_RE.match(k)):
                if dk in config:
                    console.print(f"  {dk}: modified")
                else:
                    console.print(f"  {dk}: add {changes[dk]}")

            for dk, size in sorted(resizes.items()):
                console.print(f"  {dk}: resize to {size}")

            for nk in sorted(k for k in changes if _CT_NET_RE.match(k)):
                if nk in config:
                    console.print(f"  {nk}: modified")
                else:
                    console.print(f"  {nk}: add")

            for key in sorted(deletes):
                console.print(f"  {key}: remove")

            if not confirm("Apply these changes?"):
                print_cancelled()
                return

            # Apply
            api_params = {}
            for k, v in changes.items():
                api_params[k] = (1 if v else 0) if isinstance(v, bool) else v
            if deletes:
                api_params["delete"] = ",".join(sorted(deletes))

            if api_params:
                await client.update_container_config(node, vmid=ctid, **api_params)

            for disk, size in resizes.items():
                await client.resize_container_disk(node, vmid=ctid, disk=disk, size=size)

            if pool_change:
                old_pool, new_pool = pool_change
                if old_pool:
                    await client.put(f"/pools/{old_pool}", data={"vms": str(ctid), "delete": 1})
                if new_pool:
                    await client.put(f"/pools/{new_pool}", data={"vms": str(ctid), "allow-move": 1})

            print_success(f"Container {ctid} configuration updated")

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# Helper function to get container node
async def _get_container_node(client: ProxmoxClient, ctid: int) -> tuple[str, str]:
    """Get container node and status.

    Args:
        client: Proxmox client
        ctid: Container ID

    Returns:
        Tuple of (node, status)

    Raises:
        typer.Exit: If container not found
    """
    resources = await client.get_cluster_resources(resource_type="vm")
    ct_resource = next(
        (r for r in resources if r.get("vmid") == ctid and r.get("type") == "lxc"), None
    )

    if not ct_resource:
        print_error(f"Container {ctid} not found")
        raise typer.Exit(1)

    node = ct_resource.get("node")
    if not node:
        print_error(f"Could not determine node for container {ctid}")
        raise typer.Exit(1)

    return node, ct_resource.get("status", "unknown")


async def _select_ct(client: ProxmoxClient) -> int | None:
    """Interactive container selection menu. Returns CTID or None if cancelled."""
    cts = await client.get_containers()
    if not cts:
        print_info("No containers found")
        return None
    cts = sorted(cts, key=lambda x: x.get("vmid", 0))
    items = [f"{ct.get('vmid')} - {ct.get('name', 'unnamed')} ({ct.get('status', 'unknown')})" for ct in cts]
    idx = select_menu(items, "  Select a container:")
    if idx is None:
        return None
    return cts[idx].get("vmid")


async def _select_cts(client: ProxmoxClient) -> list[int] | None:
    """Interactive multi-container selection menu. Returns list of CTIDs or None if cancelled."""
    cts = await client.get_containers()
    if not cts:
        print_info("No containers found")
        return None
    cts = sorted(cts, key=lambda x: x.get("vmid", 0))
    items = [f"{ct.get('vmid')} - {ct.get('name', 'unnamed')} ({ct.get('status', 'unknown')})" for ct in cts]
    indices = multi_select_menu(items, "  Select container(s):")
    if not indices:
        return None
    return [cts[i].get("vmid") for i in indices]


@app.command("start")
@async_to_sync
async def start_container(
    ctids: str = typer.Argument(None, help="Container ID(s) - single or comma-separated (e.g., 100 or 100,101,102)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Start one or more containers."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if ctids is None:
                ctid_list = await _select_cts(client)
                if not ctid_list:
                    print_cancelled()
                    return
            else:
                ctid_list = parse_id_list(ctids, "CT")

            cts = await validate_resources(client, ctid_list, "lxc", "Container")

            # Start containers
            started_count = 0
            skipped_count = 0

            for ct_info in cts:
                ctid = ct_info["id"]
                node = ct_info["node"]
                ct_status = ct_info["status"]
                upid = None

                if ct_status == "running":
                    print_warning(f"Container {ctid} is already running")
                    skipped_count += 1
                    continue

                try:
                    upid = await run_with_spinner(
                        client, node,
                        f"Starting container {ctid}...",
                        client.start_container(node, vmid=ctid),
                        wait_desc=f"Waiting for container {ctid} to start...",
                    )

                    print_success(f"Container {ctid} started successfully")
                    started_count += 1

                except (KeyboardInterrupt, asyncio.CancelledError):
                    if upid and node:
                        print_warning("Stopping task...")
                        await client.stop_task(node, upid)
                    print_cancelled()
                    print_info("Check Proxmox to verify task status")
                    raise typer.Exit(1)

            # Summary for multiple containers
            if len(ctid_list) > 1:
                print_info(f"Summary: {started_count} started, {skipped_count} skipped")

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("stop")
@async_to_sync
async def stop_container(
    ctids: str = typer.Argument(None, help="Container ID(s) - single or comma-separated (e.g., 100 or 100,101,102)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    timeout: int = typer.Option(None, "--timeout", "-t", help="Timeout in seconds"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Stop one or more containers (hard stop)."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if ctids is None:
                ctid_list = await _select_cts(client)
                if not ctid_list:
                    print_cancelled()
                    return
            else:
                ctid_list = parse_id_list(ctids, "CT")

            if not confirm_action(ctid_list, "Hard stop", "container", yes):
                return

            cts = await validate_resources(client, ctid_list, "lxc", "Container")

            # Stop containers
            stopped_count = 0
            skipped_count = 0

            for ct_info in cts:
                ctid = ct_info["id"]
                node = ct_info["node"]
                ct_status = ct_info["status"]
                upid = None

                if ct_status != "running":
                    print_warning(f"Container {ctid} is not running")
                    skipped_count += 1
                    continue

                try:
                    upid = await run_with_spinner(
                        client, node,
                        f"Stopping container {ctid}...",
                        client.stop_container(node, vmid=ctid, timeout=timeout),
                        wait_desc=f"Waiting for container {ctid} to stop...",
                    )

                    print_success(f"Container {ctid} stopped successfully")
                    stopped_count += 1

                except (KeyboardInterrupt, asyncio.CancelledError):
                    if upid and node:
                        print_warning("Stopping task...")
                        await client.stop_task(node, upid)
                    print_cancelled()
                    print_info("Check Proxmox to verify task status")
                    raise typer.Exit(1)

            # Summary for multiple containers
            if len(ctid_list) > 1:
                print_info(f"Summary: {stopped_count} stopped, {skipped_count} skipped")

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("shutdown")
@async_to_sync
async def shutdown_container(
    ctids: str = typer.Argument(None, help="Container ID(s) - single or comma-separated (e.g., 100 or 100,101,102)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    timeout: int = typer.Option(60, "--timeout", "-t", help="Timeout before force stop"),
    force: bool = typer.Option(False, "--force", help="Force stop after timeout"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Shutdown one or more containers gracefully."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if ctids is None:
                ctid_list = await _select_cts(client)
                if not ctid_list:
                    print_cancelled()
                    return
            else:
                ctid_list = parse_id_list(ctids, "CT")

            if not confirm_action(ctid_list, "Shutdown", "container", yes):
                return

            cts = await validate_resources(client, ctid_list, "lxc", "Container")

            # Shutdown containers
            shutdown_count = 0
            skipped_count = 0

            for ct_info in cts:
                ctid = ct_info["id"]
                node = ct_info["node"]
                ct_status = ct_info["status"]
                upid = None

                if ct_status != "running":
                    print_warning(f"Container {ctid} is not running")
                    skipped_count += 1
                    continue

                try:
                    upid = await run_with_spinner(
                        client, node,
                        f"Shutting down container {ctid}...",
                        client.shutdown_container(node, vmid=ctid, timeout=timeout, force_stop=force),
                        wait_desc=f"Waiting for container {ctid} to shutdown...",
                    )

                    print_success(f"Container {ctid} shutdown successfully")
                    shutdown_count += 1

                except (KeyboardInterrupt, asyncio.CancelledError):
                    if upid and node:
                        print_warning("Stopping task...")
                        await client.stop_task(node, upid)
                    print_cancelled()
                    print_info("Check Proxmox to verify task status")
                    raise typer.Exit(1)

            # Summary for multiple containers
            if len(ctid_list) > 1:
                print_info(f"Summary: {shutdown_count} shutdown, {skipped_count} skipped")

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("reboot")
@async_to_sync
async def reboot_container(
    ctids: str = typer.Argument(None, help="Container ID(s) - single or comma-separated (e.g., 100 or 100,101,102)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    timeout: int = typer.Option(None, "--timeout", "-t", help="Timeout in seconds"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Reboot one or more containers."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if ctids is None:
                ctid_list = await _select_cts(client)
                if not ctid_list:
                    print_cancelled()
                    return
            else:
                ctid_list = parse_id_list(ctids, "CT")

            if not confirm_action(ctid_list, "Reboot", "container", yes):
                return

            cts = await validate_resources(client, ctid_list, "lxc", "Container")

            # Reboot containers
            rebooted_count = 0
            skipped_count = 0

            for ct_info in cts:
                ctid = ct_info["id"]
                node = ct_info["node"]
                ct_status = ct_info["status"]
                upid = None

                if ct_status != "running":
                    print_warning(f"Container {ctid} is not running")
                    skipped_count += 1
                    continue

                try:
                    upid = await run_with_spinner(
                        client, node,
                        f"Rebooting container {ctid}...",
                        client.reboot_container(node, vmid=ctid, timeout=timeout),
                        wait_desc=f"Waiting for container {ctid} to reboot...",
                    )

                    print_success(f"Container {ctid} rebooted successfully")
                    rebooted_count += 1

                except (KeyboardInterrupt, asyncio.CancelledError):
                    if upid and node:
                        print_warning("Stopping task...")
                        await client.stop_task(node, upid)
                    print_cancelled()
                    print_info("Check Proxmox to verify task status")
                    raise typer.Exit(1)

            # Summary for multiple containers
            if len(ctid_list) > 1:
                print_info(f"Summary: {rebooted_count} rebooted, {skipped_count} skipped")

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("clone")
@async_to_sync
async def clone_container(
    ctid: int = typer.Argument(None, help="Source container ID (CTID)"),
    newid: int = typer.Argument(None, help="New container ID"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    hostname: str = typer.Option(None, "--hostname", "-hn", help="New container hostname"),
    target: str = typer.Option(None, "--target", "-ta", help="Target node"),
    full: bool = typer.Option(False, "--full", "-fu", help="Create full clone (not linked)"),
    pool: str = typer.Option(None, "--pool", help="Add to pool"),
    storage: str = typer.Option(None, "--storage", "-s", help="Target storage"),
    cores: int = typer.Option(None, "--cores", "-co", help="CPU cores"),
    memory: int = typer.Option(None, "--memory", "-me", help="Memory (MB)"),
    description: str = typer.Option(None, "--description", "-de", help="Container description"),
    snapname: str = typer.Option(None, "--snap", help="Clone from snapshot"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for operation to complete"),
) -> None:
    """Clone a container with optional interactive mode.

    Examples:
        pvecli ct clone 101 102                                  # Interactive mode
        pvecli ct clone 101 102 --hostname my-container          # With hostname
        pvecli ct clone 101 102 --full --target node2            # Full clone to another node
    """
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if ctid is None:
                ctid = await _select_ct(client)
                if ctid is None:
                    print_cancelled()
                    return
            # Find source node and get config
            node, _ = await _get_container_node(client, ctid)
            source_config = await client.get_container_config(node, vmid=ctid)

            # Get resources once
            resources = await client.get_cluster_resources(resource_type="vm")

            # If newid not provided, enter interactive mode
            if newid is None:
                console.print("\n[bold cyan]═══ Container Clone Wizard ═══[/bold cyan]\n")

                # Find next available ID via Proxmox API
                next_ctid = await client.get_next_vmid()

                while True:
                    try:
                        newid_input = prompt("New container ID", default=str(next_ctid))
                        newid = int(newid_input)
                        break
                    except ValueError:
                        print_error(f"Invalid container ID: '{newid_input}'. Must be a number.")

            # Check if newid already exists
            new_ct_exists = any(r.get("vmid") == newid for r in resources)
            if new_ct_exists:
                print_error(f"Container {newid} already exists")
                raise typer.Exit(1)

            clone_params = {
                "node": node,
                "vmid": ctid,
                "newid": newid,
            }

            # Interactive mode if only ctid and newid are provided
            if not any([hostname, target, pool, storage, cores, memory, description, snapname, full]):
                # Get default values
                default_hostname = source_config.get("hostname", f"ct-{newid}")
                default_target = node

                console.print("\n[bold cyan]─── Clone Parameters ───[/bold cyan]\n")

                hostname = prompt("Hostname", default=default_hostname)
                if not hostname:
                    hostname = default_hostname

                target_input = prompt("Target node", default=default_target)
                if target_input and target_input != node:
                    target = target_input

                if confirm("Create full clone?", default=False):
                    full = True

                # Pool selection
                pools = await client.get_pools()
                if pools:
                    pool_options = ["(none)"] + [p.get("poolid", "") for p in pools]
                    console.print("\n[bold]Pool:[/bold]")
                    pool_idx = select_menu(pool_options, "Select pool:")
                    if pool_idx and pool_idx > 0:
                        pool = pool_options[pool_idx]

                # Tag selection
                cluster_opts = await client.get_cluster_options()
                known_tags = set()
                for r in resources:
                    for t in r.get("tags", "").split(";"):
                        t = t.strip()
                        if t:
                            known_tags.add(t)
                cm = _parse_color_map(cluster_opts.get("tag-style", ""))
                known_tags.update(cm)

                if known_tags:
                    tag_list = sorted(known_tags)
                    entries = ["(none)"] + tag_list + ["+ Add custom tag"]
                    console.print("\n[bold]Tags:[/bold]")
                    sel = multi_select_menu(entries, "  Tags (Space to toggle, Enter to confirm):")
                    if sel is not None:
                        chosen = [entries[i] for i in sel]
                        result_tags = [t for t in chosen if t not in ("(none)", "+ Add custom tag")]
                        if "+ Add custom tag" in chosen:
                            custom = Prompt.ask("  Custom tag name")
                            if custom and custom.strip():
                                result_tags.append(custom.strip())
                        if result_tags:
                            clone_params["tags"] = ";".join(sorted(result_tags))

                # Get source container storage - try to find storage from container config
                source_storage = None
                rootfs = source_config.get("rootfs", "")
                if rootfs:
                    # rootfs format is typically "storage:content/path" or just "storage"
                    if ":" in rootfs:
                        source_storage = rootfs.split(":")[0]
                    else:
                        source_storage = rootfs

                # Storage selection
                storages = await client.get_storage_list(node)
                if storages:
                    storage_names = [s.get("storage", "") for s in storages]
                    # Pre-select source storage
                    storage_items = []
                    default_idx = 0
                    for i, sn in enumerate(storage_names):
                        suffix = " (source)" if sn == source_storage else ""
                        storage_items.append(f"{sn}{suffix}")
                        if sn == source_storage:
                            default_idx = i
                    console.print("\n[bold]Storage:[/bold]")
                    storage_idx = select_menu(storage_items, "Select storage:")
                    if storage_idx is not None:
                        storage = storage_names[storage_idx]

                default_description = f"Cloned from {ctid} to {newid}"
                description = prompt("Description", default=default_description)
                description = description if description else None

                # CPU configuration
                default_cores = source_config.get("cores", "")
                cores_input = prompt("CPU cores", default=str(default_cores) if default_cores else "")
                if cores_input:
                    cores = int(cores_input)

                # Memory configuration
                default_memory = source_config.get("memory", "")
                memory_input = prompt("Memory (MB)", default=str(default_memory) if default_memory else "")
                if memory_input:
                    memory = int(memory_input)

            # Add optional parameters if provided
            if hostname:
                clone_params["hostname"] = hostname
            if target:
                clone_params["target"] = target
            if full:
                clone_params["full"] = full
            if pool:
                clone_params["pool"] = pool
            if storage:
                clone_params["storage"] = storage
            if cores:
                clone_params["cores"] = cores
            if memory:
                clone_params["memory"] = memory
            if description:
                clone_params["description"] = description
            if snapname:
                clone_params["snapname"] = snapname

            # Display summary
            clone_type = "full" if full else "linked"
            print_info(f"\nClone Summary:")
            print_info(f"  Source:      {ctid}")
            print_info(f"  Destination: {newid}")
            print_info(f"  Type:        {clone_type}")
            if hostname:
                print_info(f"  Hostname:    {hostname}")
            if target and target != node:
                print_info(f"  Target Node: {target}")
            if pool:
                print_info(f"  Pool:        {pool}")
            if storage:
                print_info(f"  Storage:     {storage}")
            if description:
                print_info(f"  Description: {description}")
            if clone_params.get("tags"):
                print_info(f"  Tags:        {clone_params['tags']}")

            if not confirm("\nProceed with clone?", default=True):
                print_cancelled()
                raise typer.Exit()

            # Extract tags (not supported by clone API, applied after)
            clone_tags = clone_params.pop("tags", None)

            try:
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=console,
                ) as progress:
                    progress.add_task(
                        description=f"Cloning container {ctid} to {newid} ({clone_type})...",
                        total=None,
                    )
                    upid = await client.clone_container(**clone_params)

                    if wait:
                        progress.update(0, description=f"Waiting for clone to complete...")
                        await client.wait_for_task(node, upid, timeout=600)

                # Apply tags after clone
                if clone_tags:
                    target_node = clone_params.get("target", node)
                    await client.update_container_config(target_node, newid, tags=clone_tags)

                print_success(f"Container {ctid} cloned to {newid} successfully")
            except PVECliError as e:
                raise

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("remove")
@async_to_sync
async def delete_container(
    ctids: str = typer.Argument(None, help="Container ID(s) - single or comma-separated (e.g., 100 or 100,101,102)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    purge: bool = typer.Option(False, "--purge", help="Remove from backup/HA config"),
    force: bool = typer.Option(False, "--force", "-f", help="Force stop container before deletion"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for operation to complete"),
) -> None:
    """Delete one or more containers."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if ctids is None:
                ctid_list = await _select_cts(client)
                if not ctid_list:
                    print_cancelled()
                    return
            else:
                ctid_list = parse_id_list(ctids, "CT")
            cts = await validate_resources(client, ctid_list, "lxc", "Container")

            if not confirm_action(ctid_list, "Delete", "container ... This cannot be undone!", yes):
                return

            # Delete containers
            deleted_count = 0
            failed_count = 0

            for ct_info in cts:
                ctid = ct_info["id"]
                node = ct_info["node"]
                ct_status = ct_info["status"]

                try:
                    # Stop container if running and force is enabled
                    if ct_status == "running":
                        if not force:
                            print_error(f"Container {ctid} is running. Stop it first or use --force.")
                            failed_count += 1
                            continue

                        await run_with_spinner(
                            client, node,
                            f"Stopping container {ctid}...",
                            client.stop_container(node, vmid=ctid),
                            wait_desc=f"Waiting for container to stop...",
                        )

                    # Delete container
                    await run_with_spinner(
                        client, node,
                        f"Deleting container {ctid}...",
                        client.delete_container(node, ctid, purge=purge),
                        wait_desc=f"Waiting for deletion to complete..." if wait else None,
                    )

                    print_success(f"Container {ctid} deleted successfully")
                    deleted_count += 1

                except PVECliError as e:
                    print_error(f"Failed to delete container {ctid}: {str(e)}")
                    failed_count += 1

            # Summary
            if len(ctid_list) > 1:
                print_info(f"\nSummary: {deleted_count} deleted, {failed_count} failed")

            if failed_count > 0:
                raise typer.Exit(1)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# Tag subcommand group
tag_app = typer.Typer(help="Manage container tags", no_args_is_help=True, cls=ordered_group(["add", "remove", "list"]))
app.add_typer(tag_app, name="tag")


@tag_app.command("list")
@async_to_sync
async def list_tags(
    ctid: int = typer.Argument(None, help="Container ID (CTID)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """List all tags for a container."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if ctid is None:
                ctid = await _select_ct(client)
                if ctid is None:
                    print_cancelled()
                    return
            node, _ = await _get_container_node(client, ctid)
            await shared_list_tags(
                client, ctid, "container",
                get_config=lambda: client.get_container_config(node, vmid=ctid),
                node=node,
            )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@tag_app.command("add")
@async_to_sync
async def add_tag(
    ctid: int = typer.Argument(None, help="Container ID (CTID)"),
    tags: str = typer.Argument(None, help="Tag(s) to add (comma-separated)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    replace: bool = typer.Option(False, "--replace", "-re", help="Replace all existing tags instead of appending"),
) -> None:
    """Add one or more tags to a container.

    By default, tags are appended to existing tags.
    Use --replace to replace all existing tags.
    """
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if ctid is None:
                ctid = await _select_ct(client)
                if ctid is None:
                    print_cancelled()
                    return
            node, _ = await _get_container_node(client, ctid)
            await shared_add_tag(
                client, ctid, "container", node, tags, replace,
                get_config=lambda: client.get_container_config(node, vmid=ctid),
                update_config=lambda **kw: client.update_container_config(node, ctid, **kw),
            )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@tag_app.command("remove")
@async_to_sync
async def remove_tag(
    ctid: int = typer.Argument(None, help="Container ID (CTID)"),
    tags: str = typer.Argument(None, help="Tag(s) to remove (comma-separated)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Remove one or more tags from a container."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if ctid is None:
                ctid = await _select_ct(client)
                if ctid is None:
                    print_cancelled()
                    return
            node, _ = await _get_container_node(client, ctid)
            await shared_remove_tag(
                client, ctid, "container", node, tags,
                get_config=lambda: client.get_container_config(node, vmid=ctid),
                update_config=lambda **kw: client.update_container_config(node, ctid, **kw),
            )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# Snapshot subcommand group
snapshot_app = typer.Typer(help="Manage container snapshots", no_args_is_help=True, cls=ordered_group(["add", "remove", "rollback", "list"]))
app.add_typer(snapshot_app, name="snapshot")


@snapshot_app.command("list")
@async_to_sync
async def list_snapshots(
    ctid: int = typer.Argument(None, help="Container ID (CTID)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """List container snapshots."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if ctid is None:
                ctid = await _select_ct(client)
                if ctid is None:
                    print_cancelled()
                    return
            node, _ = await _get_container_node(client, ctid)
            await shared_list_snapshots(
                client, ctid, "Container", node,
                get_snapshots=lambda: client.get_container_snapshots(node, vmid=ctid),
                show_vmstate=False,
            )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@snapshot_app.command("add")
@async_to_sync
async def create_snapshot(
    ctid: int = typer.Argument(None, help="Container ID (CTID)"),
    name: str = typer.Argument(None, help="Snapshot name"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    description: str = typer.Option(None, "--description", "-de", help="Snapshot description"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for operation to complete"),
) -> None:
    """Create a container snapshot."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if ctid is None:
                ctid = await _select_ct(client)
                if ctid is None:
                    print_cancelled()
                    return
            if name is None:
                name = prompt("Snapshot name")
                if not name or not name.strip():
                    print_cancelled()
                    return
                name = name.strip()
            node, _ = await _get_container_node(client, ctid)
            await shared_create_snapshot(
                client, ctid, "Container", node, name, description, wait,
                create_fn=lambda: client.create_container_snapshot(node, ctid, name, description=description),
                always_wait=True,
            )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@snapshot_app.command("rollback")
@async_to_sync
async def rollback_snapshot(
    ctid: int = typer.Argument(None, help="Container ID (CTID)"),
    name: str = typer.Argument(None, help="Snapshot name"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for operation to complete"),
    reboot: bool = typer.Option(False, "--reboot", "-rb", help="Reboot container after rollback"),
) -> None:
    """Rollback container to a snapshot."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if ctid is None:
                ctid = await _select_ct(client)
                if ctid is None:
                    print_cancelled()
                    return
            node, _ = await _get_container_node(client, ctid)
            if name is None:
                snapshots = await client.get_container_snapshots(node, ctid)
                snaps = [s for s in snapshots if s.get("name") != "current"]
                if not snaps:
                    print_info(f"No snapshots found for container {ctid}")
                    return
                items = [f"{s.get('name', '')} - {s.get('description', '') or 'No description'}" for s in snaps]
                idx = select_menu(items, "  Select snapshot to rollback:")
                if idx is None:
                    print_cancelled()
                    return
                name = snaps[idx].get("name", "")
            await shared_rollback_snapshot(
                client, ctid, "Container", node, name, yes, wait, reboot,
                rollback_fn=lambda: client.rollback_container_snapshot(node, ctid, name),
                get_status_fn=lambda: client.get_container_status(node, vmid=ctid),
                start_fn=lambda: client.start_container(node, vmid=ctid),
                reboot_fn=lambda: client.reboot_container(node, vmid=ctid),
            )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@snapshot_app.command("remove")
@async_to_sync
async def delete_snapshot(
    ctid: int = typer.Argument(None, help="Container ID (CTID)"),
    name: str = typer.Argument(None, help="Snapshot name"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for operation to complete"),
) -> None:
    """Delete a container snapshot."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if ctid is None:
                ctid = await _select_ct(client)
                if ctid is None:
                    print_cancelled()
                    return
            node, _ = await _get_container_node(client, ctid)
            if name is None:
                snapshots = await client.get_container_snapshots(node, ctid)
                snaps = [s for s in snapshots if s.get("name") != "current"]
                if not snaps:
                    print_info(f"No snapshots found for container {ctid}")
                    return
                items = [f"{s.get('name', '')} - {s.get('description', '') or 'No description'}" for s in snaps]
                idx = select_menu(items, "  Select snapshot to remove:")
                if idx is None:
                    print_cancelled()
                    return
                name = snaps[idx].get("name", "")
            await shared_delete_snapshot(
                client, ctid, "Container", node, name, yes, wait,
                delete_fn=lambda: client.delete_container_snapshot(node, ctid, name),
            )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
@app.command("add")
def create_container(
    node: str = typer.Argument(None, help="Node name"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    ctid: int = typer.Option(None, "--ctid", "-c", help="Container ID (auto-assigned if not specified)"),
    hostname: str = typer.Option(None, "--hostname", "-ho", help="Container hostname"),
    pool: str = typer.Option(None, "--pool", "-po", help="Pool name"),
    onboot: bool = typer.Option(None, "--onboot/--no-onboot", "-ob", help="Start at boot"),
    template_storage: str = typer.Option(None, "--template-storage", "-ts", help="Storage for template"),
    template: str = typer.Option(None, "--template", "-t", help="Template file name (from template-storage)"),
    unprivileged: bool = typer.Option(True, "--unprivileged/--privileged", "-u/-pr", help="Unprivileged container"),
    password: str = typer.Option(None, "--password", "-pw", help="Root password"),
    cores: int = typer.Option(None, "--cores", "-co", help="Number of CPU cores"),
    memory: int = typer.Option(None, "--memory", "-me", help="RAM in MiB"),
    swap: int = typer.Option(None, "--swap", "-s", help="Swap in MiB"),
    rootfs_storage: str = typer.Option(None, "--rootfs-storage", "-rs", help="Storage for root filesystem"),
    rootfs_size: int = typer.Option(None, "--rootfs-size", "-rz", help="Root filesystem size in GB"),
    bridge: str = typer.Option(None, "--bridge", "-b", help="Network bridge"),
    ip: str = typer.Option(None, "--ip", "-i", help="IPv4 configuration: dhcp, cidr (e.g., 192.168.1.100/24), or none"),
    gateway: str = typer.Option(None, "--gateway", "-gw", help="IPv4 gateway address"),
    ip6: str = typer.Option(None, "--ip6", "-i6", help="IPv6 configuration: dhcp, auto, cidr (e.g., 2001:db8::1/64), or none"),
    gateway6: str = typer.Option(None, "--gateway6", "-gw6", help="IPv6 gateway address"),
    vlan: str = typer.Option(None, "--vlan", "-v", help="VLAN tag"),
    firewall: bool = typer.Option(None, "--firewall/--no-firewall", "-fw", help="Enable firewall"),
    keyctl: bool = typer.Option(None, "--keyctl/--no-keyctl", "-k", help="Enable keyctl"),
    fuse: bool = typer.Option(None, "--fuse/--no-fuse", "-f", help="Enable FUSE"),
) -> None:
    """Create a new container interactively or with options."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        # Node selection if not provided
        if node is None:
            async def _pick_node():
                async with ProxmoxClient(profile_config) as client:
                    return await client.get_nodes()

            nodes = asyncio.run(_pick_node())
            if not nodes:
                print_error("No nodes found")
                raise typer.Exit(1)
            nodes = sorted(nodes, key=lambda x: x.get("node", ""))
            node_items = [
                f"{n.get('node', '')} ({n.get('status', 'unknown')})"
                for n in nodes
            ]
            if len(nodes) == 1:
                node = nodes[0].get("node", "")
            else:
                console.print("[bold]Node:[/bold]")
                node_idx = select_menu(node_items, "  Select a node:")
                if node_idx is None:
                    print_cancelled()
                    return
                node = nodes[node_idx].get("node", "")

        # Check if we have all required arguments for non-interactive mode
        has_required_args = all([hostname, template_storage, template])

        if has_required_args:
            # Non-interactive mode with arguments
            config: dict[str, Any] = {}

            # Helper function to run async code
            async def get_data():
                async with ProxmoxClient(profile_config) as client:
                    data = {
                        "next_vmid": await client.get_next_vmid(),
                    }
                    # Get templates to validate
                    if template_storage:
                        data["templates"] = await client.get_storage_content(node, template_storage, "vztmpl")
                    return data

            data = asyncio.run(get_data())

            # CTID
            if ctid is None:
                config["vmid"] = data["next_vmid"]
            else:
                config["vmid"] = ctid

            # Required parameters
            config["hostname"] = hostname

            # Optional basic parameters
            if pool:
                config["pool"] = pool
            config["onboot"] = 1 if onboot else 0

            # Template configuration
            # If template already contains full path (storage:vztmpl/...), use it as-is
            if ":" in template:
                config["ostemplate"] = template
            else:
                # Find the template in storage
                templates = data.get("templates", [])
                template_match = None

                for tmpl in templates:
                    volid = tmpl.get("volid", "")
                    # Check if template name matches
                    if template in volid or volid.endswith(template):
                        template_match = volid
                        break

                if template_match:
                    config["ostemplate"] = template_match
                else:
                    # Fallback to constructed path
                    config["ostemplate"] = f"{template_storage}:vztmpl/{template}"
                    print_warning(f"Template '{template}' not found in storage '{template_storage}', using constructed path")

            # Container type
            config["unprivileged"] = 1 if unprivileged else 0

            # Password
            if password is not None:
                if not password or len(password) < 5:
                    print_error("Password must be at least 5 characters long")
                    raise typer.Exit(1)
                config["password"] = password

            # CPU configuration
            config["cores"] = cores if cores else 1

            # Memory configuration
            memory_value = memory if memory else 512
            config["memory"] = memory_value

            swap_value = swap if swap else 512
            config["swap"] = swap_value

            # Root filesystem
            if rootfs_storage and rootfs_size:
                config["rootfs"] = f"{rootfs_storage}:{rootfs_size}"
            elif rootfs_storage:
                config["rootfs"] = f"{rootfs_storage}:8"  # Default 8GB

            # Network configuration
            if bridge:
                net_config = f"name=eth0,bridge={bridge}"

                # IPv4 configuration
                if ip:
                    if ip.lower() == "dhcp":
                        net_config += ",ip=dhcp"
                    elif ip.lower() == "none":
                        pass  # No IPv4 configuration
                    else:
                        # Assume CIDR format
                        net_config += f",ip={ip}"
                        if gateway:
                            net_config += f",gw={gateway}"

                # IPv6 configuration
                if ip6:
                    if ip6.lower() == "dhcp":
                        net_config += ",ip6=dhcp"
                    elif ip6.lower() == "auto":
                        net_config += ",ip6=auto"
                    elif ip6.lower() == "none":
                        pass  # No IPv6 configuration
                    else:
                        # Assume CIDR format
                        net_config += f",ip6={ip6}"
                        if gateway6:
                            net_config += f",gw6={gateway6}"

                # VLAN
                if vlan:
                    net_config += f",tag={vlan}"

                # Firewall
                if firewall:
                    net_config += ",firewall=1"

                config["net0"] = net_config

            # Features (nesting always enabled)
            features = ["nesting=1"]
            if keyctl:
                features.append("keyctl=1")
            if fuse:
                features.append("fuse=1")

            config["features"] = ",".join(features)

            # Create container
            async def create():
                async with ProxmoxClient(profile_config) as client:
                    ct_id = config.pop("vmid")
                    upid = await client.create_container(node, ct_id, **config)
                    console.print(f"\n[cyan]Creating container {ct_id}...[/cyan]")
                    await client.wait_for_task(node, upid, timeout=300)
                    return ct_id

            created_ctid = asyncio.run(create())
            print_success(f"Container {created_ctid} created successfully!")
            return

        # Mixed interactive mode - ask only for missing parameters
        # Helper function to run async code
        async def get_data():
            async with ProxmoxClient(profile_config) as client:
                resources = await client.get_cluster_resources(resource_type="vm")
                cluster_opts = await client.get_cluster_options()
                return {
                    "next_vmid": await client.get_next_vmid(),
                    "pools": await client.get_pools(),
                    "storages": await client.get_storage_list(node),
                    "bridges": await client.get_network_interfaces(node),
                    "resources": resources,
                    "cluster_options": cluster_opts,
                }

        data = asyncio.run(get_data())

        # Configuration dict
        config: dict[str, Any] = {}

        console.print("\n[bold cyan]═══ Container Creation Wizard ═══[/bold cyan]\n")

        # 1. CTID
        if ctid is not None:
            config["vmid"] = ctid
        else:
            default_ctid = data["next_vmid"]
            ctid_input = Prompt.ask(
                "[bold]CTID[/bold]",
                default=str(default_ctid),
            )
            config["vmid"] = int(ctid_input)

        # 2. Hostname
        if hostname:
            config["hostname"] = hostname
        else:
            hostname_input = ""
            while not hostname_input or not hostname_input.strip():
                hostname_input = Prompt.ask("[bold]Hostname[/bold]")
                if not hostname_input or not hostname_input.strip():
                    print_error("Hostname cannot be empty")
            config["hostname"] = hostname_input.strip()

        # 3. Pool
        if pool:
            config["pool"] = pool
        else:
            pools = data["pools"]
            if pools:
                pool_options = ["(none)"] + [p.get("poolid", "") for p in pools]
                console.print("\n[bold]Pool:[/bold]")
                pool_idx = select_menu(pool_options, "Select pool:")
                if pool_idx and pool_idx > 0:
                    config["pool"] = pool_options[pool_idx]

        # 3b. Tags

        known_tags = set()
        for r in data["resources"]:
            for t in r.get("tags", "").split(";"):
                t = t.strip()
                if t:
                    known_tags.add(t)
        cm = _parse_color_map(data["cluster_options"].get("tag-style", ""))
        known_tags.update(cm)

        if known_tags:
            tag_list = sorted(known_tags)
            entries = ["(none)"] + tag_list + ["+ Add custom tag"]
            console.print("\n[bold]Tags:[/bold]")
            sel = multi_select_menu(entries, "  Tags (Space to toggle, Enter to confirm):")
            if sel is not None:
                chosen = [entries[i] for i in sel]
                result_tags = [t for t in chosen if t not in ("(none)", "+ Add custom tag")]
                if "+ Add custom tag" in chosen:
                    custom = Prompt.ask("  Custom tag name")
                    if custom and custom.strip():
                        result_tags.append(custom.strip())
                if result_tags:
                    config["tags"] = ";".join(sorted(result_tags))
        else:
            custom = Prompt.ask("[bold]Tag[/bold] (leave empty for none)", default="")
            if custom and custom.strip():
                config["tags"] = custom.strip()

        # 4. Start at boot
        if onboot is not None:
            config["onboot"] = 1 if onboot else 0
        else:
            config["onboot"] = 1 if Confirm.ask("[bold]Start at boot?[/bold]", default=False) else 0

        # 5. Template Selection
        if template_storage and template:
            # Use provided arguments
            async def get_templates():
                async with ProxmoxClient(profile_config) as client:
                    return await client.get_storage_content(node, template_storage, "vztmpl")

            templates = asyncio.run(get_templates())

            # Find the template
            template_match = None
            for tmpl in templates:
                volid = tmpl.get("volid", "")
                if ":" in template:
                    # Full volid provided
                    if volid == template:
                        template_match = volid
                        break
                else:
                    # Template name provided
                    if template in volid or volid.endswith(template):
                        template_match = volid
                        break

            if template_match:
                config["ostemplate"] = template_match
            else:
                config["ostemplate"] = f"{template_storage}:vztmpl/{template}"
        else:
            # Interactive template selection
            console.print("\n[bold cyan]─── Template Configuration ───[/bold cyan]\n")

            # Get template storages
            template_storages = [s for s in data["storages"] if "vztmpl" in s.get("content", "").split(",")]

            if not template_storages:
                print_error("No storage with container template content found")
                raise typer.Exit(1)

            storage_names = [s.get("storage", "") for s in template_storages]
            console.print("[bold]Template Storage:[/bold]")
            storage_idx = select_menu(storage_names, "Select storage for template:")
            if storage_idx is None:
                print_error("No storage selected")
                raise typer.Exit(1)

            selected_storage = storage_names[storage_idx]

            # Get templates from selected storage
            async def get_templates():
                async with ProxmoxClient(profile_config) as client:
                    return await client.get_storage_content(node, selected_storage, "vztmpl")

            templates = asyncio.run(get_templates())

            if not templates:
                print_error(f"No templates found in storage {selected_storage}")
                raise typer.Exit(1)

            template_names = [tmpl.get("volid", "").split("/")[-1] for tmpl in templates]
            console.print(f"\n[bold]Template from {selected_storage}:[/bold]")
            template_idx = select_menu(template_names, "Select template:")
            if template_idx is None:
                print_error("No template selected")
                raise typer.Exit(1)

            selected_template = templates[template_idx].get("volid", "")
            config["ostemplate"] = selected_template

        # 6. Unprivileged
        if unprivileged is not None:
            config["unprivileged"] = 1 if unprivileged else 0
        else:
            console.print("\n[bold cyan]─── Container Type ───[/bold cyan]\n")
            config["unprivileged"] = 1 if Confirm.ask("[bold]Unprivileged container?[/bold]", default=True) else 0

        # 7. Password
        if password is not None:
            if not password or len(password) < 5:
                print_error("Password must be at least 5 characters long")
                raise typer.Exit(1)
            config["password"] = password
        else:
            console.print("\n[bold cyan]─── Authentication ───[/bold cyan]\n")
            import getpass
            while True:
                password = getpass.getpass("Root password: ")

                # Check that password is not empty
                if not password:
                    print_error("Password cannot be empty")
                    continue

                # Check minimum length
                if len(password) < 5:
                    print_error("Password must be at least 5 characters long")
                    continue

                password_confirm = getpass.getpass("Confirm password: ")
                if password == password_confirm:
                    config["password"] = password
                    break
                else:
                    print_error("Passwords do not match")

        # 8. CPU
        if cores is not None:
            config["cores"] = cores
        else:
            console.print("\n[bold cyan]─── CPU Configuration ───[/bold cyan]\n")
            config["cores"] = IntPrompt.ask("Number of CPU cores", default=1)

        # 9. RAM
        if memory is not None:
            config["memory"] = memory
        else:
            console.print("\n[bold]Memory Configuration:[/bold]")
            memory_value = IntPrompt.ask("RAM (MiB)", default=512)
            config["memory"] = memory_value

        # 10. Swap
        if swap is not None:
            config["swap"] = swap
        else:
            swap_value = IntPrompt.ask("Swap (MiB)", default=512)
            config["swap"] = swap_value

        # 11. Root filesystem
        if rootfs_storage and rootfs_size:
            config["rootfs"] = f"{rootfs_storage}:{rootfs_size}"
        else:
            console.print("\n[bold cyan]─── Storage Configuration ───[/bold cyan]\n")
            storage_names_all = [s.get("storage", "") for s in data["storages"]]
            console.print("[bold]Root filesystem Storage:[/bold]")
            rootfs_idx = select_menu(storage_names_all, "Select storage for root filesystem:")
            if rootfs_idx is not None:
                rootfs_storage = storage_names_all[rootfs_idx]
                rootfs_size = IntPrompt.ask("Root filesystem size (GB)", default=8)
                config["rootfs"] = f"{rootfs_storage}:{rootfs_size}"

        # 12. Network
        if bridge:
            # Use provided network configuration
            net_config = f"name=eth0,bridge={bridge}"

            # IPv4 configuration
            if ip:
                if ip.lower() == "dhcp":
                    net_config += ",ip=dhcp"
                elif ip.lower() == "none":
                    pass  # No IPv4 configuration
                else:
                    # Assume CIDR format
                    net_config += f",ip={ip}"
                    if gateway:
                        net_config += f",gw={gateway}"

            # IPv6 configuration
            if ip6:
                if ip6.lower() == "dhcp":
                    net_config += ",ip6=dhcp"
                elif ip6.lower() == "auto":
                    net_config += ",ip6=auto"
                elif ip6.lower() == "none":
                    pass  # No IPv6 configuration
                else:
                    # Assume CIDR format
                    net_config += f",ip6={ip6}"
                    if gateway6:
                        net_config += f",gw6={gateway6}"

            # VLAN
            if vlan:
                net_config += f",tag={vlan}"

            # Firewall
            if firewall:
                net_config += ",firewall=1"

            config["net0"] = net_config
        else:
            # Interactive network configuration
            console.print("\n[bold cyan]─── Network Configuration ───[/bold cyan]\n")
            bridges = [b for b in data["bridges"] if b.get("type") == "bridge"]

            if bridges:
                bridge_names = [b.get("iface", "") for b in bridges]
                console.print("[bold]Bridge:[/bold]")
                bridge_idx = select_menu(bridge_names, "Select bridge:")
                if bridge_idx is not None:
                    bridge = bridge_names[bridge_idx]

                    # Build net0 config
                    net_config = f"name=eth0,bridge={bridge}"

                    # IPv4 configuration
                    console.print("\n[bold]IPv4 Configuration:[/bold]")
                    ip_modes = ["DHCP", "Static IP", "None"]
                    ip_idx = select_menu(ip_modes, "Select IPv4 mode:")

                    if ip_idx == 0:  # DHCP
                        net_config += ",ip=dhcp"
                    elif ip_idx == 1:  # Static
                        ip_address = Prompt.ask("IPv4 address (CIDR format, e.g., 192.168.1.100/24)")
                        net_config += f",ip={ip_address}"
                        gateway = Prompt.ask("IPv4 gateway (optional)", default="")
                        if gateway:
                            net_config += f",gw={gateway}"

                    # IPv6 configuration
                    console.print("\n[bold]IPv6 Configuration:[/bold]")
                    ip6_modes = ["DHCP", "Auto (SLAAC)", "Static IP", "None"]
                    ip6_idx = select_menu(ip6_modes, "Select IPv6 mode:")

                    if ip6_idx == 0:  # DHCP
                        net_config += ",ip6=dhcp"
                    elif ip6_idx == 1:  # Auto (SLAAC)
                        net_config += ",ip6=auto"
                    elif ip6_idx == 2:  # Static
                        ip6_address = Prompt.ask("IPv6 address (CIDR format, e.g., 2001:db8::1/64)")
                        net_config += f",ip6={ip6_address}"
                        gateway6 = Prompt.ask("IPv6 gateway (optional)", default="")
                        if gateway6:
                            net_config += f",gw6={gateway6}"
                    # If None (ip6_idx == 3), don't add IPv6 configuration

                    # VLAN
                    vlan = Prompt.ask("\nVLAN tag (leave empty for none)", default="")
                    if vlan:
                        net_config += f",tag={vlan}"

                    # Firewall
                    if Confirm.ask("Enable firewall?", default=False):
                        net_config += ",firewall=1"

                    config["net0"] = net_config

        # 13. Features (nesting always enabled)
        features = ["nesting=1"]
        if keyctl:
            features.append("keyctl=1")
        elif keyctl is None:
            # Ask only if not provided
            console.print("\n[bold cyan]─── Container Features ───[/bold cyan]\n")
            if Confirm.ask("Enable keyctl?", default=False):
                features.append("keyctl=1")

        if fuse:
            features.append("fuse=1")
        elif fuse is None:
            # Ask only if not provided
            if Confirm.ask("Enable FUSE?", default=False):
                features.append("fuse=1")

        config["features"] = ",".join(features)

        # Summary
        console.print("\n[bold cyan]═══ Configuration Summary ═══[/bold cyan]\n")
        console.print(f"[bold]CTID:[/bold] {config['vmid']}")
        console.print(f"[bold]Hostname:[/bold] {config['hostname']}")
        if "pool" in config:
            console.print(f"[bold]Pool:[/bold] {config['pool']}")
        if "tags" in config:
            console.print(f"[bold]Tags:[/bold] {config['tags']}")
        console.print(f"[bold]Template:[/bold] {config['ostemplate']}")
        console.print(f"[bold]Type:[/bold] {'Unprivileged' if config.get('unprivileged') else 'Privileged'}")
        console.print(f"[bold]CPU:[/bold] {config['cores']} core(s)")
        console.print(f"[bold]Memory:[/bold] {config['memory']} MiB")
        console.print(f"[bold]Swap:[/bold] {config['swap']} MiB")
        if "rootfs" in config:
            console.print(f"[bold]Root FS:[/bold] {config['rootfs']}")
        if "net0" in config:
            console.print(f"[bold]Network:[/bold] {config['net0']}")

        console.print()

        if not Confirm.ask("[bold]Create container with this configuration?[/bold]", default=True):
            print_cancelled()
            return

        # Create container
        async def create():
            async with ProxmoxClient(profile_config) as client:
                vmid = config.pop("vmid")
                upid = await client.create_container(node, vmid, **config)
                console.print(f"\n[cyan]Creating container...[/cyan]")
                console.print(f"[cyan]Task ID:[/cyan] {upid}")
                await client.wait_for_task(node, upid, timeout=300)
                return vmid

        created_ctid = asyncio.run(create())

        print_success(f"Container {created_ctid} created successfully!")

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    except KeyboardInterrupt:
        console.print()
        print_cancelled()
        raise typer.Exit(0)


# Template subcommand group
template_app = typer.Typer(help="Manage container templates", no_args_is_help=True, cls=ordered_group(["add", "remove", "list"]))
app.add_typer(template_app, name="template")


@template_app.command("list")
@async_to_sync
async def list_templates(
    node: str = typer.Argument(None, help="Node name"),
    storage: str = typer.Argument(None, help="Storage name"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """List all container templates in a storage."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if node is None:
                nodes = await client.get_nodes()
                node_names = sorted(n.get("node", "") for n in nodes if n.get("node"))
                if not node_names:
                    print_info("No nodes found")
                    return
                if len(node_names) == 1:
                    node = node_names[0]
                else:
                    console.print("[bold]Node:[/bold]")
                    idx = select_menu(node_names, "  Select node:")
                    if idx is None:
                        print_cancelled()
                        return
                    node = node_names[idx]

            if storage is None:
                storages = await client.get_storage_list(node)
                tmpl_storages = [s for s in storages if "vztmpl" in s.get("content", "").split(",")]
                storage_ids = sorted(s.get("storage", "") for s in tmpl_storages if s.get("storage"))
                if not storage_ids:
                    print_info(f"No template storage found on node '{node}'")
                    return
                if len(storage_ids) == 1:
                    storage = storage_ids[0]
                else:
                    console.print("[bold]Storage:[/bold]")
                    idx = select_menu(storage_ids, "  Select storage:")
                    if idx is None:
                        print_cancelled()
                        return
                    storage = storage_ids[idx]

            templates = await client.get_storage_content(node, storage, "vztmpl")

            if not templates:
                print_info(f"No templates found in storage '{storage}'")
                return

            table = create_table(
                "Templates in " + storage,
                columns=[
                    ("Name", "cyan"),
                    ("Size", "green"),
                ],
            )

            for tmpl in templates:
                volid = tmpl.get("volid", "")
                name = volid.split("/")[-1] if "/" in volid else volid
                size = format_bytes(tmpl.get("size", 0))

                table.add_row(name, size)

            console.print(table)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@template_app.command("add")
@async_to_sync
async def add_template(
    node: str = typer.Argument(None, help="Node name"),
    storage: str = typer.Argument(None, help="Storage name"),
    name: str = typer.Option(None, "--name", help="Template filename to download"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Download a container template from the Proxmox template repository."""
    import subprocess

    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if node is None:
                nodes = await client.get_nodes()
                node_names = sorted(n.get("node", "") for n in nodes if n.get("node"))
                if not node_names:
                    print_info("No nodes found")
                    return
                console.print("[bold]Node:[/bold]")
                idx = select_menu(node_names, "  Select node:")
                if idx is None:
                    print_cancelled()
                    return
                node = node_names[idx]

            if storage is None:
                storages = await client.get_storage_list(node)
                tmpl_storages = [s for s in storages if "vztmpl" in s.get("content", "").split(",")]
                storage_ids = sorted(s.get("storage", "") for s in tmpl_storages if s.get("storage"))
                if not storage_ids:
                    print_info(f"No template storage found on node '{node}'")
                    return
                if len(storage_ids) == 1:
                    storage = storage_ids[0]
                else:
                    console.print("[bold]Storage:[/bold]")
                    idx = select_menu(storage_ids, "  Select storage:")
                    if idx is None:
                        print_cancelled()
                        return
                    storage = storage_ids[idx]

            # If no template specified, use fzf to select
            if not name:
                # Get available templates
                console.print("[bold cyan]Fetching available templates...[/bold cyan]")
                available_templates = await client.get_available_templates(node)

                if not available_templates:
                    # Fallback: ask user to provide template name manually
                    console.print("[yellow]Could not fetch templates from repository[/yellow]")
                    console.print("[dim]You can provide the exact template filename manually[/dim]")
                    console.print("[dim]Example: debian-12-standard_12.7-1_amd64.tar.zst[/dim]\n")
                    name = Prompt.ask("[bold]Template filename[/bold]")

                    if not name:
                        print_error("Template filename is required")
                        raise typer.Exit(1)
                else:
                    # Extract template names and keep mapping to full template data
                    template_display = []
                    template_map = {}

                    for tmpl in available_templates:
                        tmpl_name = tmpl.get("template", "")
                        if tmpl_name:
                            template_display.append(tmpl_name)
                            template_map[tmpl_name] = tmpl

                    if not template_display:
                        print_error("No templates found in repository")
                        raise typer.Exit(1)

                    # Use fzf for multi-selection with fuzzy search
                    try:
                        template_list = "\n".join(template_display)
                        result = subprocess.run(
                            ["fzf", "-m", "--preview", "echo {}"],
                            input=template_list,
                            capture_output=True,
                            text=True,
                            timeout=60,
                        )

                        if result.returncode != 0:
                            print_cancelled()
                            return

                        selected_display = result.stdout.strip().split("\n")
                        selected_display = [n for n in selected_display if n]  # Remove empty lines

                        if not selected_display:
                            print_info("No templates selected")
                            return

                        # Map back to full template data
                        selected_names = [(name, template_map[name]) for name in selected_display]

                    except FileNotFoundError:
                        print_error("fzf is not installed. Please install fzf to use template selection.")
                        print_info("Install with: sudo apt install fzf (or brew install fzf on macOS)")
                        raise typer.Exit(1)
                    except subprocess.TimeoutExpired:
                        print_error("Template selection timed out")
                        raise typer.Exit(1)
            else:
                # When using --name argument, we still need template data
                # For now, just use the name as provided
                selected_names = [(name, None)]

            # Download the selected templates
            for template_name, template_data in selected_names:
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=console,
                ) as progress:
                    progress.add_task(description=f"Downloading template '{template_name}'...", total=None)
                    upid = await client.download_template(node, storage, template_name, template_data)
                    progress.update(0, description=f"Waiting for download to complete...")
                    await client.wait_for_task(node, upid, timeout=600)

                print_success(f"Template '{template_name}' downloaded successfully to '{storage}'")

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@template_app.command("remove")
@async_to_sync
async def remove_template(
    node: str = typer.Argument(None, help="Node name"),
    storage: str = typer.Argument(None, help="Storage name"),
    name: str = typer.Option(None, "--name", help="Template filename to remove"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Remove a container template from storage."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if node is None:
                nodes = await client.get_nodes()
                node_names = sorted(n.get("node", "") for n in nodes if n.get("node"))
                if not node_names:
                    print_info("No nodes found")
                    return
                console.print("[bold]Node:[/bold]")
                idx = select_menu(node_names, "  Select node:")
                if idx is None:
                    print_cancelled()
                    return
                node = node_names[idx]

            if storage is None:
                storages = await client.get_storage_list(node)
                tmpl_storages = [s for s in storages if "vztmpl" in s.get("content", "").split(",")]
                storage_ids = sorted(s.get("storage", "") for s in tmpl_storages if s.get("storage"))
                if not storage_ids:
                    print_info(f"No template storage found on node '{node}'")
                    return
                if len(storage_ids) == 1:
                    storage = storage_ids[0]
                else:
                    console.print("[bold]Storage:[/bold]")
                    idx = select_menu(storage_ids, "  Select storage:")
                    if idx is None:
                        print_cancelled()
                        return
                    storage = storage_ids[idx]

            # Get existing templates
            templates = await client.get_storage_content(node, storage, "vztmpl")

            if not templates:
                print_error(f"No templates found in storage '{storage}'")
                raise typer.Exit(1)

            # If no template specified, show menu
            if not name:
                template_names = [tmpl.get("volid", "").split("/")[-1] for tmpl in templates]
                console.print(f"\n[bold]Templates in {storage}:[/bold]")
                template_idx = select_menu(template_names, "Select template to remove:")

                if template_idx is None:
                    print_cancelled()
                    return

                selected_template = templates[template_idx]
                name = template_names[template_idx]
                volume = selected_template.get("volid", "")
            else:
                # Find the template by name
                selected_template = None
                volume = None
                for tmpl in templates:
                    volid = tmpl.get("volid", "")
                    template_name = volid.split("/")[-1] if "/" in volid else volid
                    if template_name == name:
                        selected_template = tmpl
                        volume = volid
                        break

                if not selected_template:
                    print_error(f"Template '{name}' not found in storage '{storage}'")
                    raise typer.Exit(1)

            # Confirmation
            if not yes:
                if not confirm(f"Remove template '{name}' from storage '{storage}'? This cannot be undone!", default=False):
                    print_cancelled()
                    return

            # Remove template
            await client.delete_storage_content(node, storage, volume)
            print_success(f"Template '{name}' removed successfully from '{storage}'")

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("vnc")
@async_to_sync
async def ct_vnc(
    ctid: int = typer.Argument(None, help="Container ID"),
    no_background: bool = typer.Option(False, "--no-background", "-b", is_flag=True, help="Run VNC server in foreground (blocking)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Open an authenticated VNC console for a container."""
    from ..utils import open_browser_window
    from ..utils.network import find_free_port
    from ..vnc.server import VNCProxyServer

    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if ctid is None:
                ctid = await _select_ct(client)
                if ctid is None:
                    print_cancelled()
                    return
            cts = await client.get_containers()
            ct = next((c for c in cts if c.get("vmid") == ctid), None)

            if not ct:
                print_error(f"Container {ctid} not found")
                raise typer.Exit(1)

            node = ct.get("node")
            ct_name = ct.get("name", "").strip()
            ct_status = ct.get("status", "unknown")

            if ct_status != "running":
                print_error(
                    f"Container {ctid} ({ct_name}) is not running (status: {ct_status}). "
                    "Start the container before opening a VNC console."
                )
                raise typer.Exit(1)

            vnc_data = await client.create_ct_vncproxy(node, ctid, websocket=True)

            host = resolve_node_host(profile_config)

            server_config = {
                "proxmox_host": host,
                "proxmox_port": profile_config.port,
                "ws_path": f"/api2/json/nodes/{node}/lxc/{ctid}/vncwebsocket",
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

        if no_background:
            print_success(f"Opening VNC console for CT {ctid} ({ct_name})...")
            console.print("[dim]Press Enter to stop the server[/dim]")
            await server.run()
        else:
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
            print_success(f"VNC console for CT {ctid} ({ct_name}) running in background (PID: {proc.pid})")

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("ssh")
@async_to_sync
async def ct_ssh(
    ctid: int = typer.Argument(None, help="Container ID"),
    user: str = typer.Option(None, "--user", "-u", help="SSH user"),
    port: int = typer.Option(None, "--port", "-P", help="SSH port"),
    key: str = typer.Option(None, "--key", "-i", help="Path to SSH key"),
    jump: bool = typer.Option(False, "--jump", "-j", is_flag=True, help="Use node as jump host"),
    command: str = typer.Option(None, "--command", "-c", help="Execute command instead of shell"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """SSH into a container (IP resolved via interfaces API)."""
    from ..ssh import build_ssh_command, exec_ssh
    from ..utils.network import resolve_ct_ip

    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if ctid is None:
                ctid = await _select_ct(client)
                if ctid is None:
                    print_cancelled()
                    return
            cts = await client.get_containers()
            ct = next((c for c in cts if c.get("vmid") == ctid), None)

            if not ct:
                print_error(f"Container {ctid} not found")
                raise typer.Exit(1)

            if ct.get("status") != "running":
                print_error(f"Container {ctid} is not running")
                raise typer.Exit(1)

            node = ct.get("node")
            ip = await resolve_ct_ip(client, node, ctid)

        ssh_user = user or profile_config.ssh_user or "root"
        ssh_port = port or profile_config.ssh_port
        ssh_key = key or profile_config.ssh_key

        jump_host = None
        if jump:
            node_host = resolve_node_host(profile_config)
            jump_user = profile_config.ssh_user or "root"
            jump_host = f"{jump_user}@{node_host}"

        args = build_ssh_command(ip, ssh_user, ssh_port, ssh_key, jump=jump_host, command=command)
        console.print(f"[dim]Connecting to {ssh_user}@{ip}...[/dim]")
        exec_ssh(args)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
