"""VM (QEMU) management commands."""

import asyncio
import re
import shlex
import time
from typing import Any

import typer
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from ..api.client import ProxmoxClient
from ..api.exceptions import PVECliError
from ..config import ConfigManager
from ..utils import (
    confirm,
    console,
    format_bytes,
    format_percentage,
    format_tags_colored,
    format_uptime,
    get_status_color,
    multi_select_menu,
    print_cancelled,
    print_error,
    print_info,
    print_success,
    print_warning,
    prompt,
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
    "start", "stop", "shutdown", "reboot", "suspend", "resume",
    "add", "clone", "edit", "remove",
    "tag", "snapshot",
    "vnc", "ssh", "rdp", "exec",
    "list", "show",
]


app = typer.Typer(help="Manage virtual machines (QEMU)", no_args_is_help=True, cls=ordered_group(_CMD_ORDER))


async def _get_vm_node(client: ProxmoxClient, vmid: int) -> tuple[str, str]:
    """Get VM node and status. Returns (node, status). Exits if not found."""
    resources = await client.get_cluster_resources(resource_type="vm")
    vm_resource = next(
        (r for r in resources if r.get("vmid") == vmid and r.get("type") == "qemu"), None
    )
    if not vm_resource:
        print_error(f"VM {vmid} not found")
        raise typer.Exit(1)
    node = vm_resource.get("node")
    if not node:
        print_error(f"Could not determine node for VM {vmid}")
        raise typer.Exit(1)
    return node, vm_resource.get("status", "unknown")


async def _select_vm(client: ProxmoxClient) -> int | None:
    """Interactive VM selection menu. Returns VMID or None if cancelled."""
    vms = await client.get_vms()
    if not vms:
        print_info("No VMs found")
        return None
    vms = sorted(vms, key=lambda x: x.get("vmid", 0))
    items = [f"{vm.get('vmid')} - {vm.get('name', 'unnamed')} ({vm.get('status', 'unknown')})" for vm in vms]
    idx = select_menu(items, "  Select a VM:")
    if idx is None:
        return None
    return vms[idx].get("vmid")


async def _select_vms(client: ProxmoxClient) -> list[int] | None:
    """Interactive multi-VM selection menu. Returns list of VMIDs or None if cancelled."""
    vms = await client.get_vms()
    if not vms:
        print_info("No VMs found")
        return None
    vms = sorted(vms, key=lambda x: x.get("vmid", 0))
    items = [f"{vm.get('vmid')} - {vm.get('name', 'unnamed')} ({vm.get('status', 'unknown')})" for vm in vms]
    indices = multi_select_menu(items, "  Select VM(s):")
    if not indices:
        return None
    return [vms[i].get("vmid") for i in indices]


@app.command("list")
@async_to_sync
async def list_vms(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    node: str = typer.Option(None, "--node", "-n", help="Filter by node"),
    status: str = typer.Option(None, "--status", "-s", help="Filter by status (running, stopped)"),
) -> None:
    """List all VMs."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            vms = await client.get_vms(node=node)

            if not vms:
                print_info("No VMs found")
                return

            # Filter by status if specified
            if status:
                vms = [vm for vm in vms if vm.get("status", "").lower() == status.lower()]

            if not vms:
                print_info(f"No VMs found with status '{status}'")
                return

            # Get tag color map

            cluster_opts = await client.get_cluster_options()
            color_map = _parse_color_map(cluster_opts.get("tag-style", ""))

            # Sort by vmid
            vms = sorted(vms, key=lambda x: x.get("vmid", 0))

            table = Table(title="Virtual Machines", show_header=True, header_style="bold cyan")
            table.add_column("VMID", style="cyan", justify="right")
            table.add_column("Name")
            table.add_column("Node")
            table.add_column("Status")
            table.add_column("CPU")
            table.add_column("Memory")
            table.add_column("Disk")
            table.add_column("Uptime")
            table.add_column("Tags")

            for vm in vms:
                vmid = str(vm.get("vmid", "-"))
                name = vm.get("name", "-")
                tags = vm.get("tags", "")
                node_name = vm.get("node", "-")
                vm_status = vm.get("status", "unknown")
                status_color = get_status_color(vm_status)

                if vm_status == "running":
                    cpu_usage = vm.get("cpu", 0) * 100
                    maxcpu = vm.get("maxcpu", vm.get("cpus", 1))
                    cpu_str = usage_bar(cpu_usage, label=f"({maxcpu}c)")

                    mem = vm.get("mem", 0)
                    maxmem = vm.get("maxmem", 1)
                    mem_percent = (mem / maxmem * 100) if maxmem else 0
                    mem_str = usage_bar(mem_percent, label=format_bytes(maxmem))

                    disk = vm.get("disk", 0)
                    maxdisk = vm.get("maxdisk", 1)
                    disk_percent = (disk / maxdisk * 100) if maxdisk else 0
                    disk_str = usage_bar(disk_percent, label=format_bytes(maxdisk))

                    uptime = vm.get("uptime", 0)
                    uptime_str = format_uptime(uptime) if uptime else "-"
                else:
                    maxcpu = vm.get("maxcpu", vm.get("cpus", 0))
                    cpu_str = f"[dim]- ({maxcpu}c)[/dim]" if maxcpu else "-"
                    maxmem = vm.get("maxmem", 0)
                    mem_str = f"[dim]- {format_bytes(maxmem)}[/dim]" if maxmem else "-"
                    maxdisk = vm.get("maxdisk", 0)
                    disk_str = f"[dim]- {format_bytes(maxdisk)}[/dim]" if maxdisk else "-"
                    uptime_str = "-"

                table.add_row(
                    vmid,
                    name,
                    node_name,
                    f"[{status_color}]{vm_status}[/{status_color}]",
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
async def show_vm(
    vmid: int = typer.Argument(None, help="VM ID"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Show detailed information about a VM."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if vmid is None:
                vmid = await _select_vm(client)
                if vmid is None:
                    print_cancelled()
                    return
            node, _ = await _get_vm_node(client, vmid)

            # Get detailed status, config, interfaces, and OS info
            status = await client.get_vm_status(node, vmid)
            config = await client.get_vm_config(node, vmid)
            interfaces = await client.get_vm_interfaces(node, vmid)
            osinfo = await client.get_vm_osinfo(node, vmid)

            # Build the display
            vm_name = config.get("name", status.get("name", f"VM {vmid}"))
            vm_status = status.get("status", "unknown")
            status_color = get_status_color(vm_status)

            lines = []
            lines.append("[bold]── General ──[/bold]")
            lines.append(f"[bold]Status:[/bold]      [{status_color}]{vm_status}[/{status_color}]")
            lines.append(f"[bold]Node:[/bold]        {node}")

            if vm_status == "running":
                uptime = status.get("uptime", 0)
                if uptime:
                    lines.append(f"[bold]Uptime:[/bold]      {format_uptime(uptime)}")

            lines.append("")
            lines.append("[bold]── Resources ──[/bold]")

            # CPU
            cpus = status.get("cpus", config.get("cores", 1))
            if vm_status == "running":
                cpu_usage = status.get("cpu", 0) * 100
                lines.append(f"[bold]CPU:[/bold]         {cpus} cores ({format_percentage(cpu_usage)} used)")
            else:
                lines.append(f"[bold]CPU:[/bold]         {cpus} cores")

            # Memory
            maxmem = status.get("maxmem", config.get("memory", 0) * 1024 * 1024)
            if vm_status == "running":
                mem = status.get("mem", 0)
                mem_percent = (mem / maxmem * 100) if maxmem else 0
                lines.append(
                    f"[bold]Memory:[/bold]      {format_bytes(mem)} / {format_bytes(maxmem)} "
                    f"({format_percentage(mem_percent)})"
                )
            else:
                lines.append(f"[bold]Memory:[/bold]      {format_bytes(maxmem)}")

            # Disk
            maxdisk = status.get("maxdisk", 0)
            if maxdisk and vm_status == "running":
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

            if config.get("bootdisk"):
                lines.append(f"[bold]Boot Disk:[/bold]   {config.get('bootdisk')}")

            if config.get("sockets"):
                lines.append(f"[bold]Sockets:[/bold]     {config.get('sockets')}")

            # OS Info from Guest Agent (if available)
            if osinfo:
                if osinfo.get("name"):
                    lines.append(f"[bold]OS Name:[/bold]     {osinfo.get('name')}")
                if osinfo.get("version"):
                    lines.append(f"[bold]OS Version:[/bold]  {osinfo.get('version')}")
                if osinfo.get("pretty-name"):
                    lines.append(f"[bold]Pretty Name:[/bold] {osinfo.get('pretty-name')}")
                if osinfo.get("version-id"):
                    lines.append(f"[bold]Version ID:[/bold]  {osinfo.get('version-id')}")

            # Network - show runtime interfaces + config
            has_runtime_interfaces = bool(interfaces)
            net_devices = [k for k in config.keys() if k.startswith("net")]

            if has_runtime_interfaces or net_devices:
                lines.append("")
                lines.append("[bold]── Network ──[/bold]")

                # Show runtime interface information if available
                if has_runtime_interfaces:
                    for iface in interfaces:
                        iface_name = iface.get("name", "unknown")
                        lines.append(f"[bold]{iface_name}:[/bold]")

                        # IPv4 addresses (QEMU guest-agent format: ip-addresses list)
                        if iface.get("ip-addresses"):
                            for ip_info in iface.get("ip-addresses", []):
                                if ip_info.get("ip-address-type") == "ipv4":
                                    ip = ip_info.get("ip-address", "")
                                    prefix = ip_info.get("prefix", "")
                                    if ip and prefix:
                                        lines.append(f"  IPv4: {ip}/{prefix}")
                                    elif ip:
                                        lines.append(f"  IPv4: {ip}")

                        # IPv6 addresses (QEMU guest-agent format: ip-addresses list)
                        if iface.get("ip-addresses"):
                            for ip_info in iface.get("ip-addresses", []):
                                if ip_info.get("ip-address-type") == "ipv6":
                                    ip = ip_info.get("ip-address", "")
                                    prefix = ip_info.get("prefix", "")
                                    if ip and prefix:
                                        lines.append(f"  IPv6: {ip}/{prefix}")
                                    elif ip:
                                        lines.append(f"  IPv6: {ip}")

                        # MAC address (QEMU guest-agent: hardware-address or mac-address)
                        mac = iface.get("hardware-address") or iface.get("mac-address")
                        if mac:
                            lines.append(f"  MAC:  {mac}")

                        # Alternative format: direct IPv4/IPv6 fields (like containers)
                        if iface.get("inet"):
                            lines.append(f"  IPv4: {iface.get('inet')}")
                        if iface.get("inet6"):
                            lines.append(f"  IPv6: {iface.get('inet6')}")

                # Show configuration in one line format (like CT)
                if net_devices:
                    if has_runtime_interfaces:
                        lines.append("")
                    for net_dev in sorted(net_devices):
                        net_config = config.get(net_dev, "")
                        lines.append(f"[bold]{net_dev}:[/bold] {net_config}")

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
                title=f"VM {vmid}: {vm_name}",
                border_style="blue",
            )
            console.print(panel)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


_VM_DISK_RE = re.compile(r"^(scsi|virtio|ide|sata|efidisk)\d+$")
_VM_NET_RE = re.compile(r"^net\d+$")


def _is_cdrom(val: str) -> bool:
    """Check if a disk config is a CDROM."""
    return "media=cdrom" in val


async def _edit_vm_disks(config, changes, resizes, deletes, client, node):
    """Disk sub-menu for VM edit."""

    while True:
        disk_keys = sorted(
            k for k in set(list(config) + list(changes))
            if _VM_DISK_RE.match(k) and k not in deletes
        )

        options = []
        for dk in disk_keys:
            val = str(changes.get(dk, config.get(dk, "")))
            prefix = "* " if dk in changes or dk in resizes else "  "
            size_info = f" -> {resizes[dk]}" if dk in resizes else ""
            options.append(f"{prefix}{dk.ljust(10)} {val[:50]}{size_info}")

        for dk in sorted(k for k in deletes if _VM_DISK_RE.match(k)):
            options.append(f"  {dk.ljust(10)} [removed]")

        options.append("  " + "─" * 50)
        options.append("  Add disk")
        options.append("  Mount ISO")

        # Check if there's a mounted CDROM to eject
        has_cdrom = False
        for dk in disk_keys:
            val = str(changes.get(dk, config.get(dk, "")))
            if _is_cdrom(val) and val != "none,media=cdrom":
                has_cdrom = True
                break
        if has_cdrom:
            options.append("  Eject CDROM")

        if disk_keys:
            options.append("  Remove disk")
        options.append("  Back")

        idx = select_menu(options, "\n  Disks:")

        if idx is None or options[idx].strip() == "Back":
            return

        if options[idx].strip() == "Mount ISO":
            storages = await client.get_storage_list(node)
            iso_storages = [s for s in storages if "iso" in s.get("content", "").split(",")]
            if not iso_storages:
                print_error("No storage with ISO content found")
                continue

            storage_names = [s.get("storage", "") for s in iso_storages]
            st_idx = select_menu(storage_names, "  ISO storage:")
            if st_idx is None:
                continue
            selected_storage = storage_names[st_idx]

            isos = await client.get_storage_content(node, selected_storage, "iso")
            if not isos:
                print_error(f"No ISOs found in {selected_storage}")
                continue

            iso_names = [iso.get("volid", "").split("/")[-1] for iso in isos]
            iso_idx = select_menu(iso_names, "  Select ISO:")
            if iso_idx is None:
                continue

            selected_volid = isos[iso_idx].get("volid", "")
            changes["ide2"] = f"{selected_volid},media=cdrom"
            continue

        if options[idx].strip() == "Eject CDROM":
            cdrom_keys = [
                dk for dk in disk_keys
                if _is_cdrom(str(changes.get(dk, config.get(dk, ""))))
                and str(changes.get(dk, config.get(dk, ""))) != "none,media=cdrom"
            ]
            if len(cdrom_keys) == 1:
                changes[cdrom_keys[0]] = "none,media=cdrom"
            elif cdrom_keys:
                ej_idx = select_menu(cdrom_keys + ["Cancel"], "  Eject which drive?")
                if ej_idx is not None and ej_idx < len(cdrom_keys):
                    changes[cdrom_keys[ej_idx]] = "none,media=cdrom"
            continue

        if options[idx].strip() == "Add disk":
            bus_types = ["virtio", "scsi", "ide", "sata"]
            bus_idx = select_menu(bus_types, "  Bus type:")
            if bus_idx is None:
                continue
            bus = bus_types[bus_idx]

            all_keys = set(list(config) + list(changes))
            next_i = 0
            while f"{bus}{next_i}" in all_keys:
                next_i += 1
            disk_name = f"{bus}{next_i}"

            storages = await client.get_storage_list(node)
            storage_names = [s.get("storage", "") for s in storages]
            if not storage_names:
                print_error("No storage available")
                continue

            st_idx = select_menu(storage_names, "  Storage:")
            if st_idx is None:
                continue
            storage = storage_names[st_idx]

            size = IntPrompt.ask("  Size (GB)", default=32)

            formats = ["qcow2", "raw", "vmdk"]
            fmt_idx = select_menu(formats, "  Format:")
            fmt = formats[fmt_idx] if fmt_idx is not None else "qcow2"

            changes[disk_name] = f"{storage}:{size},format={fmt}"
            if bus == "scsi" and "scsihw" not in config:
                changes["scsihw"] = "virtio-scsi-pci"
            continue

        if options[idx].strip() == "Remove disk":
            removable = list(disk_keys)
            if not removable:
                continue
            rm_idx = select_menu(removable + ["Cancel"], "  Remove disk:")
            if rm_idx is not None and rm_idx < len(removable):
                dk = removable[rm_idx]
                changes.pop(dk, None)
                resizes.pop(dk, None)
                if dk in config:
                    deletes.add(dk)
            continue

        # Selected a disk -> resize (skip CDROMs)
        if idx < len(disk_keys):
            dk = disk_keys[idx]
            val = str(changes.get(dk, config.get(dk, "")))

            if _is_cdrom(val):
                # CDROM selected - offer change ISO or eject
                cdrom_opts = ["Change ISO", "Eject", "Cancel"]
                cd_idx = select_menu(cdrom_opts, f"  {dk}: {val[:50]}")
                if cd_idx == 0:
                    # Change ISO - same flow as Mount ISO
                    storages = await client.get_storage_list(node)
                    iso_storages = [s for s in storages if "iso" in s.get("content", "").split(",")]
                    if not iso_storages:
                        print_error("No storage with ISO content found")
                        continue
                    storage_names = [s.get("storage", "") for s in iso_storages]
                    st_idx = select_menu(storage_names, "  ISO storage:")
                    if st_idx is not None:
                        selected_storage = storage_names[st_idx]
                        isos = await client.get_storage_content(node, selected_storage, "iso")
                        if isos:
                            iso_names = [iso.get("volid", "").split("/")[-1] for iso in isos]
                            iso_idx = select_menu(iso_names, "  Select ISO:")
                            if iso_idx is not None:
                                selected_volid = isos[iso_idx].get("volid", "")
                                changes[dk] = f"{selected_volid},media=cdrom"
                        else:
                            print_error(f"No ISOs found in {selected_storage}")
                elif cd_idx == 1:
                    changes[dk] = "none,media=cdrom"
                continue

            # Data disk -> resize
            current_size = resizes.get(dk, extract_size(val))

            console.print(f"\n  Current size: {current_size}")
            new_size = Prompt.ask("  New size in GB (empty to cancel)", default="")
            if new_size:
                try:
                    int(new_size)
                    resizes[dk] = f"{new_size}G"
                except ValueError:
                    print_error("Invalid number")


async def _edit_vm_network(config, changes, deletes, client, node):
    """Network sub-menu for VM edit."""


    while True:
        net_keys = sorted(
            k for k in set(list(config) + list(changes))
            if _VM_NET_RE.match(k) and k not in deletes
        )

        options = []
        for nk in net_keys:
            val = changes.get(nk, config.get(nk, ""))
            prefix = "* " if nk in changes else "  "
            options.append(f"{prefix}{nk.ljust(6)} {str(val)[:55]}")

        for nk in sorted(k for k in deletes if _VM_NET_RE.match(k)):
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

            net_config = f"virtio,bridge={bridges[br_idx]}"

            vlan = Prompt.ask("  VLAN tag (empty for none)", default="")
            if vlan:
                net_config += f",tag={vlan}"

            if select_menu(["No", "Yes"], "  Firewall:") == 1:
                net_config += ",firewall=1"

            all_keys = set(list(config) + list(changes))
            next_i = 0
            while f"net{next_i}" in all_keys:
                next_i += 1

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

            current_vlan = params.get("tag", "")
            new_vlan = Prompt.ask("  VLAN tag", default=current_vlan if current_vlan else "")
            if new_vlan:
                params["tag"] = new_vlan
            elif "tag" in params:
                del params["tag"]

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
async def edit_vm(
    vmid: int = typer.Argument(None, help="VM ID"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Interactively edit VM configuration."""


    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if vmid is None:
                vmid = await _select_vm(client)
                if vmid is None:
                    print_cancelled()
                    return
            node, _ = await _get_vm_node(client, vmid)
            config = await client.get_vm_config(node, vmid)

            # Pool info comes from cluster resources, not config
            resources = await client.get_cluster_resources(resource_type="vm")
            current_pool = next(
                (r for r in resources if r.get("vmid") == vmid), {}
            ).get("pool", "")

            # Simple fields: (api_key, label, type, default)
            fields = [
                ("name", "Name", str, ""),
                ("cores", "CPU Cores", int, 1),
                ("sockets", "CPU Sockets", int, 1),
                ("memory", "Memory (MB)", int, 512),
                ("balloon", "Min Memory (MB)", int, 0),
                ("onboot", "Start on boot", bool, False),
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
                    if _VM_DISK_RE.match(k) and k not in deletes
                )
                disk_mod = len(resizes) + len([k for k in changes if _VM_DISK_RE.match(k)]) + len([k for k in deletes if _VM_DISK_RE.match(k)])
                disk_label = f"Disks         [{', '.join(disk_keys)}]" if disk_keys else "Disks         (none)"
                options.append(f"{'* ' if disk_mod else '  '}{disk_label}")
                disks_menu_idx = len(options) - 1

                net_keys = sorted(
                    k for k in set(list(config) + list(changes))
                    if _VM_NET_RE.match(k) and k not in deletes
                )
                net_mod = len([k for k in changes if _VM_NET_RE.match(k)]) + len([k for k in deletes if _VM_NET_RE.match(k)])
                net_label = f"Network       [{', '.join(net_keys)}]" if net_keys else "Network       (none)"
                options.append(f"{'* ' if net_mod else '  '}{net_label}")
                net_menu_idx = len(options) - 1

                # Apply / Cancel
                options.append("  " + "─" * (max_label + 20))
                total = len(changes) + len(resizes) + len(deletes) + (1 if pool_change else 0)
                options.append(f"  Apply {total} change(s)" if total else "  (no changes)")
                options.append("  Cancel")

                selected = select_menu(options, f"\n  VM {vmid}: {config.get('name', '')}")

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

                if selected == disks_menu_idx:
                    await _edit_vm_disks(config, changes, resizes, deletes, client, node)
                    continue

                if selected == net_menu_idx:
                    await _edit_vm_network(config, changes, deletes, client, node)
                    continue

                # Simple field edit
                if selected < len(fields):
                    key, label, ftype, default = fields[selected]
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

            if pool_change:
                console.print(f"  Pool: {pool_change[0] or '(none)'} -> {pool_change[1] or '(none)'}")

            if "tags" in changes:
                console.print(f"  Tags: {config.get('tags', '') or '(none)'} -> {changes['tags'] or '(none)'}")

            for dk in sorted(k for k in changes if _VM_DISK_RE.match(k)):
                if dk in config:
                    console.print(f"  {dk}: modified")
                else:
                    console.print(f"  {dk}: add {changes[dk]}")

            for dk, size in sorted(resizes.items()):
                console.print(f"  {dk}: resize to {size}")

            for nk in sorted(k for k in changes if _VM_NET_RE.match(k)):
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
                await client.update_vm_config(node, vmid, **api_params)

            for disk, size in resizes.items():
                await client.resize_vm_disk(node, vmid, disk, size)

            if pool_change:
                old_pool, new_pool = pool_change
                if old_pool:
                    await client.put(f"/pools/{old_pool}", data={"vms": str(vmid), "delete": 1})
                if new_pool:
                    await client.put(f"/pools/{new_pool}", data={"vms": str(vmid), "allow-move": 1})

            print_success(f"VM {vmid} configuration updated")

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("start")
@async_to_sync
async def start_vm(
    vmids: str = typer.Argument(None, help="VM ID(s) - single or comma-separated (e.g., 100 or 100,101,102)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Start one or more VMs."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if vmids is None:
                vmid_list = await _select_vms(client)
                if not vmid_list:
                    print_cancelled()
                    return
            else:
                vmid_list = parse_id_list(vmids, "VM")

            # Validate all VMIDs
            vms = await validate_resources(client, vmid_list, "qemu", "VM")

            # Start VMs
            started_count = 0
            skipped_count = 0

            for vm_info in vms:
                vmid = vm_info["id"]
                node = vm_info["node"]
                vm_status = vm_info["status"]
                upid = None

                if vm_status == "running":
                    print_warning(f"VM {vmid} is already running")
                    skipped_count += 1
                    continue

                try:
                    upid = await run_with_spinner(
                        client, node,
                        f"Starting VM {vmid}...",
                        client.start_vm(node, vmid),
                        f"Waiting for VM {vmid} to start...",
                    )

                    print_success(f"VM {vmid} started successfully")
                    started_count += 1

                except (KeyboardInterrupt, asyncio.CancelledError):
                    if upid and node:
                        print_warning("Stopping task...")
                        await client.stop_task(node, upid)
                    print_cancelled()
                    print_info("Check Proxmox to verify task status")
                    raise typer.Exit(1)

            # Summary for multiple VMs
            if len(vmid_list) > 1:
                print_info(f"Summary: {started_count} started, {skipped_count} skipped")

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("stop")
@async_to_sync
async def stop_vm(
    vmids: str = typer.Argument(None, help="VM ID(s) - single or comma-separated (e.g., 100 or 100,101,102)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    timeout: int = typer.Option(None, "--timeout", "-t", help="Timeout in seconds"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Stop one or more VMs (hard stop)."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if vmids is None:
                vmid_list = await _select_vms(client)
                if not vmid_list:
                    print_cancelled()
                    return
            else:
                vmid_list = parse_id_list(vmids, "VM")

            # Confirmation
            if not confirm_action(vmid_list, "Hard stop", "VM", yes):
                return

            # Validate all VMIDs
            vms = await validate_resources(client, vmid_list, "qemu", "VM")

            # Stop VMs
            stopped_count = 0
            skipped_count = 0

            for vm_info in vms:
                vmid = vm_info["id"]
                node = vm_info["node"]
                vm_status = vm_info["status"]
                upid = None

                if vm_status != "running":
                    print_warning(f"VM {vmid} is not running")
                    skipped_count += 1
                    continue

                try:
                    upid = await run_with_spinner(
                        client, node,
                        f"Stopping VM {vmid}...",
                        client.stop_vm(node, vmid, timeout=timeout),
                        f"Waiting for VM {vmid} to stop...",
                        timeout=timeout,
                    )

                    print_success(f"VM {vmid} stopped successfully")
                    stopped_count += 1

                except (KeyboardInterrupt, asyncio.CancelledError):
                    if upid and node:
                        print_warning("Stopping task...")
                        await client.stop_task(node, upid)
                    print_cancelled()
                    print_info("Check Proxmox to verify task status")
                    raise typer.Exit(1)

            # Summary for multiple VMs
            if len(vmid_list) > 1:
                print_info(f"Summary: {stopped_count} stopped, {skipped_count} skipped")

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("shutdown")
@async_to_sync
async def shutdown_vm(
    vmids: str = typer.Argument(None, help="VM ID(s) - single or comma-separated (e.g., 100 or 100,101,102)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    timeout: int = typer.Option(60, "--timeout", "-t", help="Timeout before force stop"),
    force: bool = typer.Option(False, "--force", help="Force stop after timeout"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Shutdown one or more VMs gracefully."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if vmids is None:
                vmid_list = await _select_vms(client)
                if not vmid_list:
                    print_cancelled()
                    return
            else:
                vmid_list = parse_id_list(vmids, "VM")

            # Confirmation
            if not confirm_action(vmid_list, "Shutdown", "VM", yes):
                return

            # Validate all VMIDs
            vms = await validate_resources(client, vmid_list, "qemu", "VM")

            # Shutdown VMs
            shutdown_count = 0
            skipped_count = 0

            for vm_info in vms:
                vmid = vm_info["id"]
                node = vm_info["node"]
                vm_status = vm_info["status"]
                upid = None

                if vm_status != "running":
                    print_warning(f"VM {vmid} is not running")
                    skipped_count += 1
                    continue

                try:
                    upid = await run_with_spinner(
                        client, node,
                        f"Shutting down VM {vmid}...",
                        client.shutdown_vm(node, vmid, timeout=timeout, force_stop=force),
                        f"Waiting for VM {vmid} to shutdown...",
                        timeout=timeout,
                    )

                    print_success(f"VM {vmid} shutdown successfully")
                    shutdown_count += 1

                except (KeyboardInterrupt, asyncio.CancelledError):
                    if upid and node:
                        print_warning("Stopping task...")
                        await client.stop_task(node, upid)
                    print_cancelled()
                    print_info("Check Proxmox to verify task status")
                    raise typer.Exit(1)

            # Summary for multiple VMs
            if len(vmid_list) > 1:
                print_info(f"Summary: {shutdown_count} shutdown, {skipped_count} skipped")

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("reboot")
@async_to_sync
async def reboot_vm(
    vmids: str = typer.Argument(None, help="VM ID(s) - single or comma-separated (e.g., 100 or 100,101,102)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    timeout: int = typer.Option(None, "--timeout", "-t", help="Timeout in seconds"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Reboot one or more VMs."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if vmids is None:
                vmid_list = await _select_vms(client)
                if not vmid_list:
                    print_cancelled()
                    return
            else:
                vmid_list = parse_id_list(vmids, "VM")

            # Confirmation
            if not confirm_action(vmid_list, "Reboot", "VM", yes):
                return

            # Validate all VMIDs
            vms = await validate_resources(client, vmid_list, "qemu", "VM")

            # Reboot VMs
            rebooted_count = 0
            skipped_count = 0

            for vm_info in vms:
                vmid = vm_info["id"]
                node = vm_info["node"]
                vm_status = vm_info["status"]
                upid = None

                if vm_status != "running":
                    print_warning(f"VM {vmid} is not running")
                    skipped_count += 1
                    continue

                try:
                    upid = await run_with_spinner(
                        client, node,
                        f"Rebooting VM {vmid}...",
                        client.reboot_vm(node, vmid, timeout=timeout),
                        f"Waiting for VM {vmid} to reboot...",
                        timeout=timeout,
                    )

                    print_success(f"VM {vmid} rebooted successfully")
                    rebooted_count += 1

                except (KeyboardInterrupt, asyncio.CancelledError):
                    if upid and node:
                        print_warning("Stopping task...")
                        await client.stop_task(node, upid)
                    print_cancelled()
                    print_info("Check Proxmox to verify task status")
                    raise typer.Exit(1)

            # Summary for multiple VMs
            if len(vmid_list) > 1:
                print_info(f"Summary: {rebooted_count} rebooted, {skipped_count} skipped")

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("suspend")
@async_to_sync
async def suspend_vm(
    vmids: str = typer.Argument(None, help="VM ID(s) - single or comma-separated (e.g., 100 or 100,101,102)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Suspend one or more VMs."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if vmids is None:
                vmid_list = await _select_vms(client)
                if not vmid_list:
                    print_cancelled()
                    return
            else:
                vmid_list = parse_id_list(vmids, "VM")

            # Confirmation
            if not confirm_action(vmid_list, "Suspend", "VM", yes):
                return

            # Validate all VMIDs
            vms = await validate_resources(client, vmid_list, "qemu", "VM")

            # Suspend VMs
            suspended_count = 0
            skipped_count = 0

            for vm_info in vms:
                vmid = vm_info["id"]
                node = vm_info["node"]
                vm_status = vm_info["status"]
                upid = None

                if vm_status != "running":
                    print_warning(f"VM {vmid} is not running")
                    skipped_count += 1
                    continue

                try:
                    upid = await run_with_spinner(
                        client, node,
                        f"Suspending VM {vmid}...",
                        client.suspend_vm(node, vmid),
                        f"Waiting for VM {vmid} to suspend...",
                    )

                    print_success(f"VM {vmid} suspended successfully")
                    suspended_count += 1

                except (KeyboardInterrupt, asyncio.CancelledError):
                    if upid and node:
                        print_warning("Stopping task...")
                        await client.stop_task(node, upid)
                    print_cancelled()
                    print_info("Check Proxmox to verify task status")
                    raise typer.Exit(1)

            # Summary for multiple VMs
            if len(vmid_list) > 1:
                print_info(f"Summary: {suspended_count} suspended, {skipped_count} skipped")

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("resume")
@async_to_sync
async def resume_vm(
    vmids: str = typer.Argument(None, help="VM ID(s) - single or comma-separated (e.g., 100 or 100,101,102)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Resume one or more suspended VMs."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if vmids is None:
                vmid_list = await _select_vms(client)
                if not vmid_list:
                    print_cancelled()
                    return
            else:
                vmid_list = parse_id_list(vmids, "VM")

            # Confirmation
            if not confirm_action(vmid_list, "Resume", "VM", yes):
                return

            # Validate all VMIDs
            vms = await validate_resources(client, vmid_list, "qemu", "VM")

            # Resume VMs
            resumed_count = 0
            skipped_count = 0

            for vm_info in vms:
                vmid = vm_info["id"]
                node = vm_info["node"]
                vm_status = vm_info["status"]
                upid = None

                if vm_status != "suspended":
                    print_warning(f"VM {vmid} is not suspended (status: {vm_status})")
                    skipped_count += 1
                    continue

                try:
                    upid = await run_with_spinner(
                        client, node,
                        f"Resuming VM {vmid}...",
                        client.resume_vm(node, vmid),
                        f"Waiting for VM {vmid} to resume...",
                    )

                    print_success(f"VM {vmid} resumed successfully")
                    resumed_count += 1

                except (KeyboardInterrupt, asyncio.CancelledError):
                    if upid and node:
                        print_warning("Stopping task...")
                        await client.stop_task(node, upid)
                    print_cancelled()
                    print_info("Check Proxmox to verify task status")
                    raise typer.Exit(1)

            # Summary for multiple VMs
            if len(vmid_list) > 1:
                print_info(f"Summary: {resumed_count} resumed, {skipped_count} skipped")

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("exec")
@async_to_sync
async def exec_vm_command(
    vmid: int = typer.Argument(None, help="VM ID"),
    command: str = typer.Argument(None, help="Command to execute"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    timeout: int = typer.Option(30, "--timeout", "-t", help="Timeout in seconds"),
) -> None:
    """Execute a command in a VM via QEMU Guest Agent.

    Examples:
        pvecli vm exec 102 "id"
        pvecli vm exec 102 "ls -la /home"
        pvecli vm exec 102 "cat /etc/os-release"
        pvecli vm exec 102 "echo hello" --timeout 60
    """
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if vmid is None:
                vmid = await _select_vm(client)
                if vmid is None:
                    print_cancelled()
                    return
            if command is None:
                command = prompt("Command to execute")
                if not command or not command.strip():
                    print_cancelled()
                    return
                command = command.strip()
            node, vm_status = await _get_vm_node(client, vmid)

            if vm_status != "running":
                print_warning(f"VM {vmid} is not running")
                return

            # Parse command - split by spaces but respect quotes
            try:
                cmd_parts = shlex.split(command)
            except ValueError as e:
                print_error(f"Invalid command syntax: {e}")
                raise typer.Exit(1)

            if not cmd_parts:
                print_error("Command cannot be empty")
                raise typer.Exit(1)

            # Execute command
            try:
                result = await client.exec_vm_command(node, vmid, cmd_parts)
                pid = result.get("pid")
                if pid is None:
                    print_error("Failed to execute command: No PID returned")
                    raise typer.Exit(1)

                # Poll for command completion
                start_time = time.time()

                while True:
                    elapsed = time.time() - start_time
                    if elapsed > timeout:
                        print_warning(f"Command still running after {timeout}s (PID {pid})")
                        raise typer.Exit(1)

                    try:
                        status = await client.get_vm_exec_status(node, vmid, pid)

                        # Check if command has exited
                        if status.get("exited"):
                            # Command finished, display output
                            exitcode = status.get("exitcode", -1)

                            # Display stdout
                            stdout_data = status.get("out-data")
                            if stdout_data:
                                console.print(stdout_data, end="")

                            # Display stderr
                            stderr_data = status.get("err-data")
                            if stderr_data:
                                print_error("STDERR:")
                                console.print(stderr_data, end="")

                            # Display exit code
                            if exitcode == 0:
                                print_success(f"Exit code: {exitcode}")
                            else:
                                print_error(f"Exit code: {exitcode}")
                            raise typer.Exit(exitcode)

                    except PVECliError as e:
                        print_error(f"Failed to get command status: {str(e)}")
                        raise typer.Exit(1)

                    # Wait before next poll
                    await asyncio.sleep(0.2)

            except PVECliError as e:
                print_error(f"Failed to execute command: {str(e)}")
                raise typer.Exit(1)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("clone")
@async_to_sync
def clone_vm(
    vmid: int = typer.Argument(None, help="Source VM ID"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    newid: int = typer.Option(None, "--newid", "-id", help="New VM ID (auto-assigned if not specified)"),
    name: str = typer.Option(None, "--name", "-na", help="New VM name"),
    pool: str = typer.Option(None, "--pool", "-pl", help="Pool name"),
    onboot: bool = typer.Option(None, "--onboot/--no-onboot", "-ob", help="Start at boot"),
    iso_storage: str = typer.Option(None, "--iso-storage", "-iss", help="Storage for ISO"),
    iso: str = typer.Option(None, "--iso", "-i", help="ISO file name (from iso-storage)"),
    os_type: str = typer.Option(None, "--os-type", "-ot", help="OS type: linux or windows"),
    os_version: str = typer.Option(None, "--os-version", "-ov", help="OS version (e.g., '11/2022/2025', '6.x')"),
    agent: bool = typer.Option(True, "--agent/--no-agent", "-ag", help="Enable QEMU Guest Agent"),
    sockets: int = typer.Option(None, "--sockets", "-so", help="CPU sockets"),
    cores: int = typer.Option(None, "--cores", "-co", help="CPU cores per socket"),
    vcpus: int = typer.Option(None, "--vcpus", "-vc", help="vCPU count at startup"),
    cpu_type: str = typer.Option(None, "--cpu-type", "-ct", help="CPU type: x86-64-v2-AES or host"),
    memory: int = typer.Option(None, "--memory", "-me", help="RAM in MiB"),
    disk_storage: str = typer.Option(None, "--disk-storage", "-ds", help="Storage for primary disk"),
    disk_size: int = typer.Option(None, "--disk-size", "-dz", help="Disk size in GB"),
    disk_format: str = typer.Option(None, "--disk-format", "-df", help="Disk format: qcow2, raw, vmdk"),
    bridge: str = typer.Option(None, "--bridge", "-br", help="Network bridge"),
    vlan: str = typer.Option(None, "--vlan", "-vl", help="VLAN tag"),
    firewall: bool = typer.Option(None, "--firewall/--no-firewall", "-fw", help="Enable firewall"),
    link_down: bool = typer.Option(None, "--link-down/--no-link-down", "-ld", help="Start disconnected"),
    virtio_iso_storage: str = typer.Option(None, "--virtio-iso-storage", "-vis", help="Storage for VirtIO ISO (Windows only)"),
    virtio_iso: str = typer.Option(None, "--virtio-iso", "-vi", help="VirtIO ISO file name (Windows only)"),
    tpm_storage: str = typer.Option(None, "--tpm-storage", "-ts", help="Storage for TPM (Windows 11/2022/2025 only)"),
    efi_storage: str = typer.Option(None, "--efi-storage", "-es", help="Storage for EFI (Windows 11/2022/2025 only)"),
    full: bool = typer.Option(False, "--full", "-fu", help="Create full clone (not linked)"),
    target: str = typer.Option(None, "--target", "-ta", help="Target node"),
) -> None:
    """Clone a VM with optional interactive mode.

    Examples:
        pvecli vm clone 100                                  # Interactive mode
        pvecli vm clone 100 --newid 101 --name my-vm         # With name
        pvecli vm clone 100 --newid 101 --full --target node2 # Full clone to another node
    """


    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        if vmid is None:
            async def _pick_vm():
                async with ProxmoxClient(profile_config) as client:
                    return await _select_vm(client)
            vmid = asyncio.run(_pick_vm())
            if vmid is None:
                print_cancelled()
                return

        # Helper function to get source VM config and determine node
        async def get_source_vm_data():
            async with ProxmoxClient(profile_config) as client:
                resources = await client.get_cluster_resources(resource_type="vm")
                vm_resource = next((r for r in resources if r.get("vmid") == vmid), None)

                if not vm_resource:
                    raise PVECliError(f"Source VM {vmid} not found")

                source_node = vm_resource.get("node")
                source_config = await client.get_vm_config(source_node, vmid)

                return {
                    "source_node": source_node,
                    "source_config": source_config,
                    "next_vmid": await client.get_next_vmid(),
                    "pools": await client.get_pools(),
                    "storages": await client.get_storage_list(source_node),
                    "bridges": await client.get_network_interfaces(source_node),
                    "resources": resources,
                    "cluster_options": await client.get_cluster_options(),
                }

        source_data = asyncio.run(get_source_vm_data())
        source_node = source_data["source_node"]
        source_config = source_data["source_config"]

        # Check if we have enough arguments for non-interactive mode
        has_required_args = all([newid, name, iso_storage, iso, os_type])

        if has_required_args:
            # Non-interactive mode with arguments
            config: dict[str, Any] = {
                "node": source_node,
                "vmid": vmid,
                "newid": newid,
                "full": full,
            }

            # Required parameters
            config["name"] = name

            # Optional basic parameters
            if pool:
                config["pool"] = pool
            config["onboot"] = 1 if onboot else 0
            config["agent"] = 1 if agent else 0

            # ISO configuration
            config["ide2"] = f"{iso_storage}:iso/{iso},media=cdrom"

            # OS Type determination
            is_windows = os_type.lower() == "windows"

            if is_windows:
                # Windows OS type mapping
                if not os_version:
                    os_version = "11/2022/2025"  # Default

                if "11" in os_version or "2022" in os_version or "2025" in os_version:
                    config["ostype"] = "win11"
                    needs_tpm = True
                elif "10" in os_version or "2016" in os_version or "2019" in os_version:
                    config["ostype"] = "win10"
                    needs_tpm = False
                elif "8" in os_version or "2012" in os_version:
                    config["ostype"] = "win8"
                    needs_tpm = False
                elif "7" in os_version or "2008" in os_version:
                    config["ostype"] = "win7"
                    needs_tpm = False
                elif "xp" in os_version.lower() or "2003" in os_version:
                    config["ostype"] = "wxp"
                    needs_tpm = False
                elif "2000" in os_version:
                    config["ostype"] = "w2k"
                    needs_tpm = False
                else:
                    config["ostype"] = "win11"
                    needs_tpm = True

                # VirtIO drivers
                if virtio_iso_storage and virtio_iso:
                    config["ide3"] = f"{virtio_iso_storage}:iso/{virtio_iso},media=cdrom"

                # TPM for Windows 11/2022/2025
                if needs_tpm:
                    if not tpm_storage:
                        print_error("--tpm-storage is required for Windows 11/2022/2025")
                        raise typer.Exit(1)
                    if not efi_storage:
                        print_error("--efi-storage is required for Windows 11/2022/2025")
                        raise typer.Exit(1)
                    config["tpmstate0"] = f"{tpm_storage}:1,version=v2.0"
                    config["efidisk0"] = f"{efi_storage}:1,efitype=4m,pre-enrolled-keys=1"
                    config["bios"] = "ovmf"
            else:
                # Linux OS type
                if os_version and "2.4" in os_version:
                    config["ostype"] = "l24"
                else:
                    config["ostype"] = "l26"

            # CPU configuration
            config["sockets"] = sockets if sockets else 1
            config["cores"] = cores if cores else 2

            total_possible_vcpus = config["sockets"] * config["cores"]
            if vcpus and vcpus != total_possible_vcpus:
                if vcpus > total_possible_vcpus:
                    print_warning(f"vCPU count cannot exceed {total_possible_vcpus}, setting to {total_possible_vcpus}")
                    vcpus = total_possible_vcpus
                config["vcpus"] = vcpus

            config["cpu"] = cpu_type if cpu_type else "x86-64-v2-AES"

            # Memory configuration
            memory_value = memory if memory else 2048
            config["memory"] = memory_value
            config["balloon"] = memory_value

            # Disk configuration
            if disk_storage and disk_size:
                format_str = disk_format if disk_format else "qcow2"
                if is_windows:
                    config["scsi0"] = f"{disk_storage}:{disk_size},format={format_str}"
                    config["scsihw"] = "virtio-scsi-pci"
                else:
                    config["virtio0"] = f"{disk_storage}:{disk_size},format={format_str}"

            # Network configuration
            if bridge:
                net_config = f"virtio,bridge={bridge}"
                if vlan:
                    net_config += f",tag={vlan}"
                if firewall:
                    net_config += ",firewall=1"
                if link_down:
                    net_config += ",link_down=1"
                config["net0"] = net_config

            # Clone VM
            target_node = target if target else source_node

            async def clone():
                async with ProxmoxClient(profile_config) as client:
                    clone_params = {
                        "node": source_node,
                        "vmid": vmid,
                        "newid": newid,
                        "name": name,
                        "full": full,
                    }
                    if target_node != source_node:
                        clone_params["target"] = target_node
                    if pool:
                        clone_params["pool"] = pool

                    upid = await client.clone_vm(**clone_params)
                    console.print(f"\n[cyan]Cloning VM {vmid} to {newid}...[/cyan]")
                    await client.wait_for_task(source_node, upid, timeout=600)

                    # Apply additional config
                    config_to_apply = {k: v for k, v in config.items() if k not in ["node", "vmid", "newid", "name", "full"]}
                    if config_to_apply:
                        await client.update_vm_config(target_node, newid, **config_to_apply)

                    return newid

            cloned_vmid = asyncio.run(clone())
            print_success(f"VM {vmid} cloned to {cloned_vmid} successfully!")
            return

        # Interactive mode
        async def get_data():
            async with ProxmoxClient(profile_config) as client:
                return {
                    "next_vmid": source_data["next_vmid"],
                    "pools": source_data["pools"],
                    "storages": source_data["storages"],
                    "bridges": source_data["bridges"],
                }

        data = asyncio.run(get_data())

        # Configuration dict
        config: dict[str, Any] = {
            "node": source_node,
            "vmid": vmid,
        }

        console.print("\n[bold cyan]═══ VM Clone Wizard ═══[/bold cyan]\n")

        # 1. VMID
        if newid is not None:
            config["newid"] = newid
        else:
            default_vmid = data["next_vmid"]
            vmid_input = None
            while vmid_input is None:
                try:
                    vmid_str = Prompt.ask(
                        "[bold]New VMID[/bold]",
                        default=str(default_vmid),
                    )
                    config["newid"] = int(vmid_str)
                    vmid_input = True
                except ValueError:
                    print_error("VMID must be a valid number (e.g., 100, 102)")

        newid = config["newid"]

        # 2. Name
        if name:
            config["name"] = name
        else:
            default_name = source_config.get("name", f"vm-{newid}")
            vm_name = Prompt.ask("[bold]VM Name[/bold]", default=default_name)
            config["name"] = vm_name.strip() if vm_name.strip() else default_name

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

        # 5. Clone type
        if not full:
            config["full"] = 0 if Confirm.ask("[bold]Create linked clone?[/bold]", default=True) else 1
        else:
            config["full"] = 1

        # 6. Target node
        if target:
            config["target"] = target
        # else: target remains source_node, no need to set explicitly

        # 7. OS Selection
        console.print("\n[bold cyan]─── OS Configuration ───[/bold cyan]\n")

        # 7.1 & 7.2. Storage and ISO selection
        if iso_storage and iso:
            # Use provided ISO configuration
            config["ide2"] = f"{iso_storage}:iso/{iso},media=cdrom"
            selected_storage = iso_storage
        else:
            iso_storages = [s for s in data["storages"] if "iso" in s.get("content", "").split(",")]

            if not iso_storages:
                print_error("No storage with ISO content found")
                raise typer.Exit(1)

            storage_names = [s.get("storage", "") for s in iso_storages]

            if iso_storage:
                # Storage provided but not ISO
                selected_storage = iso_storage
            else:
                # Ask for storage
                console.print("[bold]ISO Storage:[/bold]")
                storage_idx = select_menu(storage_names, "Select storage for ISO:")
                if storage_idx is None:
                    print_error("No storage selected")
                    raise typer.Exit(1)
                selected_storage = storage_names[storage_idx]

            # Get ISOs from selected storage
            async def get_isos():
                async with ProxmoxClient(profile_config) as client:
                    return await client.get_storage_content(source_node, selected_storage, "iso")

            isos = asyncio.run(get_isos())

            if not isos:
                print_error(f"No ISOs found in storage {selected_storage}")
                raise typer.Exit(1)

            iso_names = [iso.get("volid", "").split("/")[-1] for iso in isos]
            console.print(f"\n[bold]ISO from {selected_storage}:[/bold]")
            iso_idx = select_menu(iso_names, "Select ISO:")
            if iso_idx is None:
                print_error("No ISO selected")
                raise typer.Exit(1)

            selected_iso = isos[iso_idx].get("volid", "")
            config["ide2"] = f"{selected_iso},media=cdrom"

        # 7.3. OS Type
        if os_type:
            is_windows = os_type.lower() == "windows"
        else:
            console.print("\n[bold]OS Type:[/bold]")
            os_types = ["Linux", "Windows"]
            os_idx = select_menu(os_types, "Select OS type:")
            is_windows = os_idx == 1

        # 7.4. OS Version
        if is_windows:
            if os_version:
                # Use provided version
                # Determine ostype based on provided version
                if "11" in os_version or "2022" in os_version or "2025" in os_version:
                    config["ostype"] = "win11"
                    needs_tpm = True
                elif "10" in os_version or "2016" in os_version or "2019" in os_version:
                    config["ostype"] = "win10"
                    needs_tpm = False
                elif "8" in os_version or "2012" in os_version:
                    config["ostype"] = "win8"
                    needs_tpm = False
                elif "7" in os_version or "2008" in os_version:
                    config["ostype"] = "win7"
                    needs_tpm = False
                elif "xp" in os_version.lower() or "2003" in os_version:
                    config["ostype"] = "wxp"
                    needs_tpm = False
                elif "2000" in os_version:
                    config["ostype"] = "w2k"
                    needs_tpm = False
                else:
                    config["ostype"] = "win11"
                    needs_tpm = True
            else:
                # Ask user for version
                win_versions = [
                    "11/2022/2025",
                    "10/2016/2019",
                    "8.x/2012/2012r2",
                    "7/2008r2",
                    "Vista/2008",
                    "XP/2003",
                    "2000",
                ]
                console.print("\n[bold]Windows Version:[/bold]")
                win_idx = select_menu(win_versions, "Select version:")
                # Determine ostype based on selection
                if win_idx == 0:  # 11/2022/2025
                    config["ostype"] = "win11"
                    needs_tpm = True
                elif win_idx == 1:  # 10/2016/2019
                    config["ostype"] = "win10"
                    needs_tpm = False
                elif win_idx == 2:  # 8.x/2012/2012r2
                    config["ostype"] = "win8"
                    needs_tpm = False
                elif win_idx == 3:  # 7/2008r2
                    config["ostype"] = "win7"
                    needs_tpm = False
                elif win_idx == 4:  # Vista/2008
                    config["ostype"] = "win7"
                    needs_tpm = False
                elif win_idx == 5:  # XP/2003
                    config["ostype"] = "wxp"
                    needs_tpm = False
                else:  # 2000
                    config["ostype"] = "w2k"
                    needs_tpm = False

            # 8.1. VirtIO Drivers
            if virtio_iso_storage and virtio_iso:
                # Use provided VirtIO ISO
                config["ide3"] = f"{virtio_iso_storage}:iso/{virtio_iso},media=cdrom"
            elif not virtio_iso and Confirm.ask("\n[bold]Mount VirtIO drivers ISO?[/bold]", default=True):
                # Ask for storage again for VirtIO ISO
                console.print("[bold]VirtIO ISO Storage:[/bold]")
                virtio_storage_idx = select_menu(storage_names, "Select storage for VirtIO ISO:")
                if virtio_storage_idx is not None:
                    virtio_selected_storage = storage_names[virtio_storage_idx]

                    # Get all ISOs from selected storage
                    async def get_virtio_isos():
                        async with ProxmoxClient(profile_config) as client:
                            return await client.get_storage_content(source_node, virtio_selected_storage, "iso")

                    virtio_isos_all = asyncio.run(get_virtio_isos())

                    if virtio_isos_all:
                        virtio_iso_names = [iso.get("volid", "").split("/")[-1] for iso in virtio_isos_all]
                        console.print(f"\n[bold]VirtIO ISO from {virtio_selected_storage}:[/bold]")
                        virtio_idx = select_menu(virtio_iso_names, "Select VirtIO ISO:")
                        if virtio_idx is not None:
                            virtio_iso = virtio_isos_all[virtio_idx].get("volid", "")
                            config["ide3"] = f"{virtio_iso},media=cdrom"
                    else:
                        print_warning(f"No ISOs found in storage {virtio_selected_storage}")

            # 8.3. TPM
            if needs_tpm:
                console.print("\n[bold cyan]TPM required for this OS[/bold cyan]")
                storage_names_all = [s.get("storage", "") for s in data["storages"]]
                console.print("[bold]TPM Storage:[/bold]")
                tpm_idx = select_menu(storage_names_all, "Select storage for TPM:")
                if tpm_idx is not None:
                    tpm_storage = storage_names_all[tpm_idx]
                    config["tpmstate0"] = f"{tpm_storage}:1,version=v2.0"

            # 8.4. EFI Disk
            if needs_tpm:
                storage_names_all = [s.get("storage", "") for s in data["storages"]]
                console.print("[bold]EFI Storage:[/bold]")
                efi_idx = select_menu(storage_names_all, "Select storage for EFI:")
                if efi_idx is not None:
                    efi_storage = storage_names_all[efi_idx]
                    config["efidisk0"] = f"{efi_storage}:1,efitype=4m,pre-enrolled-keys=1"
                    config["bios"] = "ovmf"

        else:
            # Linux
            linux_versions = [
                "6.x Kernel or 2.6 Kernel",
                "2.4 Kernel",
            ]
            console.print("\n[bold]Linux Kernel Version:[/bold]")
            linux_idx = select_menu(linux_versions, "Select kernel version:")

            # Determine ostype based on kernel version
            if linux_idx == 0:  # 6.x or 2.6 Kernel
                config["ostype"] = "l26"
            else:  # 2.4 Kernel
                config["ostype"] = "l24"

        # 9. QEMU Guest Agent
        if agent is not None:
            config["agent"] = 1 if agent else 0
        else:
            console.print("\n[bold cyan]─── Additional Configuration ───[/bold cyan]\n")
            config["agent"] = 1 if Confirm.ask("[bold]Enable QEMU Guest Agent?[/bold]", default=True) else 0

        # 10. CPU
        if sockets or cores or cpu_type:
            # At least one CPU parameter provided
            config["sockets"] = sockets if sockets else 1
            config["cores"] = cores if cores else 2
        else:
            console.print("\n[bold]CPU Configuration:[/bold]")
            config["sockets"] = IntPrompt.ask("Number of sockets", default=1)
            config["cores"] = IntPrompt.ask("Number of cores per socket", default=2)

        # Calculate total possible vCPUs
        total_possible_vcpus = config["sockets"] * config["cores"]

        # Ask for vCPU count at startup (hot-plug)
        if vcpus:
            if vcpus > total_possible_vcpus:
                print_warning(f"vCPU count cannot exceed {total_possible_vcpus}, setting to {total_possible_vcpus}")
                config["vcpus"] = total_possible_vcpus
            else:
                config["vcpus"] = vcpus
        elif vcpus is None:
            # Ask interactively
            console.print(f"\n[dim]Total vCPUs available: {total_possible_vcpus}[/dim]")
            vcpu_count = IntPrompt.ask(
                "vCPU count at startup (leave empty to use all)",
                default=total_possible_vcpus
            )
            if vcpu_count and vcpu_count != total_possible_vcpus:
                if vcpu_count > total_possible_vcpus:
                    print_warning(f"vCPU count cannot exceed {total_possible_vcpus}, setting to {total_possible_vcpus}")
                    vcpu_count = total_possible_vcpus
                config["vcpus"] = vcpu_count

        if cpu_type:
            config["cpu"] = cpu_type
        elif cpu_type is None:
            console.print("\n[bold]CPU Type:[/bold]")
            cpu_types = ["x86-64-v2-AES (default)", "host"]
            cpu_idx = select_menu(cpu_types, "Select CPU type:")
            if cpu_idx == 1:
                config["cpu"] = "host"
            else:
                config["cpu"] = "x86-64-v2-AES"

        # 11. RAM
        if memory:
            config["memory"] = memory
            config["balloon"] = memory
        elif memory is None:
            console.print("\n[bold]Memory Configuration:[/bold]")
            memory_value = IntPrompt.ask("RAM (MiB)", default=2048)
            config["memory"] = memory_value
            config["balloon"] = memory_value

        # 11.5. Primary Disk
        if disk_storage and disk_size:
            # Use provided disk configuration
            format_str = disk_format if disk_format else "qcow2"
            if is_windows:
                config["scsi0"] = f"{disk_storage}:{disk_size},format={format_str}"
                config["scsihw"] = "virtio-scsi-pci"
            else:
                config["virtio0"] = f"{disk_storage}:{disk_size},format={format_str}"
        elif disk_storage is None and disk_size is None:
            # Ask interactively
            console.print("\n[bold cyan]─── Disk Configuration ───[/bold cyan]\n")
            if Confirm.ask("[bold]Add primary disk?[/bold]", default=True):
                storage_names_all = [s.get("storage", "") for s in data["storages"]]
                console.print("[bold]Disk Storage:[/bold]")
                disk_idx = select_menu(storage_names_all, "Select storage for primary disk:")
                if disk_idx is not None:
                    disk_storage = storage_names_all[disk_idx]
                    disk_size = IntPrompt.ask("Disk size (GB)", default=32)

                    # Disk format
                    console.print("\n[bold]Disk Format:[/bold]")
                    disk_formats = ["qcow2", "raw", "vmdk"]
                    format_idx = select_menu(disk_formats, "Select disk format:")
                    disk_format = disk_formats[format_idx] if format_idx is not None else "qcow2"

                    # Use virtio for Linux, scsi for Windows
                    if is_windows:
                        config["scsi0"] = f"{disk_storage}:{disk_size},format={disk_format}"
                        config["scsihw"] = "virtio-scsi-pci"
                    else:
                        config["virtio0"] = f"{disk_storage}:{disk_size},format={disk_format}"

        # 12. Network
        if bridge:
            # Use provided network configuration
            net_config = f"virtio,bridge={bridge}"

            # VLAN
            if vlan:
                net_config += f",tag={vlan}"

            # Firewall
            if firewall:
                net_config += ",firewall=1"

            # Link state
            if link_down:
                net_config += ",link_down=1"

            config["net0"] = net_config
        elif bridge is None:
            # Ask interactively
            console.print("\n[bold cyan]─── Network Configuration ───[/bold cyan]\n")
            bridges = [b for b in data["bridges"] if b.get("type") == "bridge"]

            if bridges:
                bridge_names = [b.get("iface", "") for b in bridges]
                console.print("[bold]Bridge:[/bold]")
                bridge_idx = select_menu(bridge_names, "Select bridge:")
                if bridge_idx is not None:
                    bridge = bridge_names[bridge_idx]

                    # Build net0 config
                    net_config = f"virtio,bridge={bridge}"

                    # VLAN
                    vlan = Prompt.ask("VLAN tag (leave empty for none)", default="")
                    if vlan:
                        net_config += f",tag={vlan}"

                    # Firewall
                    if Confirm.ask("Enable firewall?", default=False):
                        net_config += ",firewall=1"

                    # Link state
                    if Confirm.ask("Start disconnected?", default=False):
                        net_config += ",link_down=1"

                    config["net0"] = net_config

        # Summary
        console.print("\n[bold cyan]═══ Configuration Summary ═══[/bold cyan]\n")
        console.print(f"[bold]Source VMID:[/bold] {vmid}")
        console.print(f"[bold]New VMID:[/bold] {config['newid']}")
        console.print(f"[bold]Name:[/bold] {config['name']}")
        if "pool" in config:
            console.print(f"[bold]Pool:[/bold] {config['pool']}")
        if "tags" in config:
            console.print(f"[bold]Tags:[/bold] {config['tags']}")
        console.print(f"[bold]Clone Type:[/bold] {'Full' if config.get('full') else 'Linked'}")
        console.print(f"[bold]CPU:[/bold] {config['sockets']} socket(s) × {config['cores']} core(s) ({config['cpu']})")
        console.print(f"[bold]Memory:[/bold] {config['memory']} MiB")
        if "net0" in config:
            console.print(f"[bold]Network:[/bold] {config['net0']}")
        console.print(f"[bold]OS Type:[/bold] {config['ostype']}")
        if "ide2" in config:
            console.print(f"[bold]ISO:[/bold] {config['ide2']}")

        console.print()

        if not Confirm.ask("[bold]Clone VM with this configuration?[/bold]", default=True):
            print_cancelled()
            return

        # Clone VM
        target_node = config.pop("target", source_node)

        async def clone():
            async with ProxmoxClient(profile_config) as client:
                clone_params = {
                    "node": source_node,
                    "vmid": vmid,
                    "newid": config.pop("newid"),
                    "name": config.pop("name"),
                    "full": config.pop("full", 0),
                }
                if target_node != source_node:
                    clone_params["target"] = target_node
                if "pool" in config:
                    clone_params["pool"] = config.pop("pool")

                upid = await client.clone_vm(**clone_params)
                console.print(f"\n[cyan]Cloning VM {vmid}...[/cyan]")
                console.print(f"[cyan]Task ID:[/cyan] {upid}")
                await client.wait_for_task(source_node, upid, timeout=600)

                # Apply remaining config
                if config:
                    await client.update_vm_config(target_node, clone_params["newid"], **config)

                return clone_params["newid"]

        cloned_vmid = asyncio.run(clone())

        print_success(f"VM {vmid} cloned to {cloned_vmid} successfully!")

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    except KeyboardInterrupt:
        console.print()
        print_cancelled()
        raise typer.Exit(0)


@app.command("remove")
@async_to_sync
async def delete_vm(
    vmids: str = typer.Argument(None, help="VM ID(s) - single or comma-separated (e.g., 100 or 100,101,102)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    purge: bool = typer.Option(False, "--purge", help="Remove from backup/HA config"),
    force: bool = typer.Option(False, "--force", "-f", help="Force stop VM before deletion"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for operation to complete"),
) -> None:
    """Delete one or more VMs."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if vmids is None:
                vmid_list = await _select_vms(client)
                if not vmid_list:
                    print_cancelled()
                    return
            else:
                vmid_list = parse_id_list(vmids, "VM")
            # Validate all VMIDs
            vms = await validate_resources(client, vmid_list, "qemu", "VM")

            # Confirmation
            if not confirm_action(vmid_list, "Delete", "VM", yes):
                return

            # Delete VMs
            deleted_count = 0
            failed_count = 0

            for vm_info in vms:
                vmid = vm_info["id"]
                node = vm_info["node"]
                vm_status = vm_info["status"]

                try:
                    # Stop VM if running and force is enabled
                    if vm_status == "running":
                        if not force:
                            print_error(f"VM {vmid} is running. Stop it first or use --force.")
                            failed_count += 1
                            continue

                        await run_with_spinner(
                            client, node,
                            f"Stopping VM {vmid}...",
                            client.stop_vm(node, vmid),
                            f"Waiting for VM to stop...",
                        )

                    # Delete VM
                    await run_with_spinner(
                        client, node,
                        f"Deleting VM {vmid}...",
                        client.delete_vm(node, vmid, purge=purge),
                        f"Waiting for deletion to complete..." if wait else None,
                    )

                    print_success(f"VM {vmid} deleted successfully")
                    deleted_count += 1

                except PVECliError as e:
                    print_error(f"Failed to delete VM {vmid}: {str(e)}")
                    failed_count += 1

            # Summary
            if len(vmid_list) > 1:
                print_info(f"\nSummary: {deleted_count} deleted, {failed_count} failed")

            if failed_count > 0:
                raise typer.Exit(1)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# Tag subcommand group
tag_app = typer.Typer(help="Manage VM tags", no_args_is_help=True, cls=ordered_group(["add", "remove", "list"]))
app.add_typer(tag_app, name="tag")


@tag_app.command("list")
@async_to_sync
async def list_tags(
    vmid: int = typer.Argument(None, help="VM ID"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """List all tags for a VM."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if vmid is None:
                vmid = await _select_vm(client)
                if vmid is None:
                    print_cancelled()
                    return
            node, _ = await _get_vm_node(client, vmid)
            await shared_list_tags(
                client, vmid, "VM",
                get_config=lambda: client.get_vm_config(node, vmid),
                node=node,
            )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@tag_app.command("add")
@async_to_sync
async def add_tag(
    vmid: int = typer.Argument(None, help="VM ID"),
    tags: str = typer.Argument(None, help="Tag(s) to add (comma-separated)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    replace: bool = typer.Option(False, "--replace", "-re", help="Replace all existing tags instead of appending"),
) -> None:
    """Add one or more tags to a VM.

    By default, tags are appended to existing tags.
    Use --replace to replace all existing tags.
    """
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if vmid is None:
                vmid = await _select_vm(client)
                if vmid is None:
                    print_cancelled()
                    return
            node, _ = await _get_vm_node(client, vmid)
            await shared_add_tag(
                client, vmid, "VM", node, tags, replace,
                get_config=lambda: client.get_vm_config(node, vmid),
                update_config=lambda **kw: client.update_vm_config(node, vmid, **kw),
            )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@tag_app.command("remove")
@async_to_sync
async def remove_tag(
    vmid: int = typer.Argument(None, help="VM ID"),
    tags: str = typer.Argument(None, help="Tag(s) to remove (comma-separated)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Remove one or more tags from a VM."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if vmid is None:
                vmid = await _select_vm(client)
                if vmid is None:
                    print_cancelled()
                    return
            node, _ = await _get_vm_node(client, vmid)
            await shared_remove_tag(
                client, vmid, "VM", node, tags,
                get_config=lambda: client.get_vm_config(node, vmid),
                update_config=lambda **kw: client.update_vm_config(node, vmid, **kw),
            )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# Snapshot subcommand group
snapshot_app = typer.Typer(help="Manage VM snapshots", no_args_is_help=True, cls=ordered_group(["add", "remove", "rollback", "list"]))
app.add_typer(snapshot_app, name="snapshot")


@snapshot_app.command("list")
@async_to_sync
async def list_snapshots(
    vmid: int = typer.Argument(None, help="VM ID"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """List VM snapshots."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if vmid is None:
                vmid = await _select_vm(client)
                if vmid is None:
                    print_cancelled()
                    return
            node, _ = await _get_vm_node(client, vmid)
            await shared_list_snapshots(
                client, vmid, "VM", node,
                get_snapshots=lambda: client.get_vm_snapshots(node, vmid),
                show_vmstate=True,
            )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@snapshot_app.command("add")
@async_to_sync
async def create_snapshot(
    vmid: int = typer.Argument(None, help="VM ID"),
    name: str = typer.Argument(None, help="Snapshot name"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    description: str = typer.Option(None, "--description", "-de", help="Snapshot description"),
    vmstate: bool = typer.Option(False, "--vmstate", help="Include RAM state"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for operation to complete"),
) -> None:
    """Create a VM snapshot."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if vmid is None:
                vmid = await _select_vm(client)
                if vmid is None:
                    print_cancelled()
                    return
            if name is None:
                name = prompt("Snapshot name")
                if not name or not name.strip():
                    print_cancelled()
                    return
                name = name.strip()
            node, _ = await _get_vm_node(client, vmid)
            await shared_create_snapshot(
                client, vmid, "VM", node, name, description, wait,
                create_fn=lambda: client.create_vm_snapshot(node, vmid, name, description=description, vmstate=vmstate),
            )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@snapshot_app.command("rollback")
@async_to_sync
async def rollback_snapshot(
    vmid: int = typer.Argument(None, help="VM ID"),
    name: str = typer.Argument(None, help="Snapshot name"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for operation to complete"),
    reboot: bool = typer.Option(False, "--reboot", "-rb", help="Reboot VM after rollback"),
) -> None:
    """Rollback VM to a snapshot."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if vmid is None:
                vmid = await _select_vm(client)
                if vmid is None:
                    print_cancelled()
                    return
            node, _ = await _get_vm_node(client, vmid)
            if name is None:
                snapshots = await client.get_vm_snapshots(node, vmid)
                snaps = [s for s in snapshots if s.get("name") != "current"]
                if not snaps:
                    print_info(f"No snapshots found for VM {vmid}")
                    return
                items = [f"{s.get('name', '')} - {s.get('description', '') or 'No description'}" for s in snaps]
                idx = select_menu(items, "  Select snapshot to rollback:")
                if idx is None:
                    print_cancelled()
                    return
                name = snaps[idx].get("name", "")
            await shared_rollback_snapshot(
                client, vmid, "VM", node, name, yes, wait, reboot,
                rollback_fn=lambda: client.rollback_vm_snapshot(node, vmid, name),
                get_status_fn=lambda: client.get_vm_status(node, vmid),
                start_fn=lambda: client.start_vm(node, vmid),
                reboot_fn=lambda: client.reboot_vm(node, vmid),
            )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@snapshot_app.command("remove")
@async_to_sync
async def delete_snapshot(
    vmid: int = typer.Argument(None, help="VM ID"),
    name: str = typer.Argument(None, help="Snapshot name"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    wait: bool = typer.Option(False, "--wait", "-w", help="Wait for operation to complete"),
) -> None:
    """Delete a VM snapshot."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if vmid is None:
                vmid = await _select_vm(client)
                if vmid is None:
                    print_cancelled()
                    return
            node, _ = await _get_vm_node(client, vmid)
            if name is None:
                snapshots = await client.get_vm_snapshots(node, vmid)
                snaps = [s for s in snapshots if s.get("name") != "current"]
                if not snaps:
                    print_info(f"No snapshots found for VM {vmid}")
                    return
                items = [f"{s.get('name', '')} - {s.get('description', '') or 'No description'}" for s in snaps]
                idx = select_menu(items, "  Select snapshot to remove:")
                if idx is None:
                    print_cancelled()
                    return
                name = snaps[idx].get("name", "")
            await shared_delete_snapshot(
                client, vmid, "VM", node, name, yes, wait,
                delete_fn=lambda: client.delete_vm_snapshot(node, vmid, name),
            )

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("add")
def create_vm(
    node: str = typer.Argument(None, help="Node name"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    vmid: int = typer.Option(None, "--vmid", "-id", help="VM ID (auto-assigned if not specified)"),
    name: str = typer.Option(None, "--name", "-na", help="VM name"),
    pool: str = typer.Option(None, "--pool", "-pl", help="Pool name"),
    onboot: bool = typer.Option(None, "--onboot/--no-onboot", "-ob", help="Start at boot"),
    iso_storage: str = typer.Option(None, "--iso-storage", "-iss", help="Storage for ISO"),
    iso: str = typer.Option(None, "--iso", "-i", help="ISO file name (from iso-storage)"),
    os_type: str = typer.Option(None, "--os-type", "-ot", help="OS type: linux or windows"),
    os_version: str = typer.Option(None, "--os-version", "-ov", help="OS version (e.g., '11/2022/2025', '6.x')"),
    agent: bool = typer.Option(True, "--agent/--no-agent", "-ag", help="Enable QEMU Guest Agent"),
    sockets: int = typer.Option(None, "--sockets", "-so", help="CPU sockets"),
    cores: int = typer.Option(None, "--cores", "-co", help="CPU cores per socket"),
    vcpus: int = typer.Option(None, "--vcpus", "-vc", help="vCPU count at startup"),
    cpu_type: str = typer.Option(None, "--cpu-type", "-ct", help="CPU type: x86-64-v2-AES or host"),
    memory: int = typer.Option(None, "--memory", "-me", help="RAM in MiB"),
    disk_storage: str = typer.Option(None, "--disk-storage", "-ds", help="Storage for primary disk"),
    disk_size: int = typer.Option(None, "--disk-size", "-dz", help="Disk size in GB"),
    disk_format: str = typer.Option(None, "--disk-format", "-df", help="Disk format: qcow2, raw, vmdk"),
    bridge: str = typer.Option(None, "--bridge", "-br", help="Network bridge"),
    vlan: str = typer.Option(None, "--vlan", "-vl", help="VLAN tag"),
    firewall: bool = typer.Option(None, "--firewall/--no-firewall", "-fw", help="Enable firewall"),
    link_down: bool = typer.Option(None, "--link-down/--no-link-down", "-ld", help="Start disconnected"),
    virtio_iso_storage: str = typer.Option(None, "--virtio-iso-storage", "-vis", help="Storage for VirtIO ISO (Windows only)"),
    virtio_iso: str = typer.Option(None, "--virtio-iso", "-vi", help="VirtIO ISO file name (Windows only)"),
    tpm_storage: str = typer.Option(None, "--tpm-storage", "-ts", help="Storage for TPM (Windows 11/2022/2025 only)"),
    efi_storage: str = typer.Option(None, "--efi-storage", "-es", help="Storage for EFI (Windows 11/2022/2025 only)"),
) -> None:
    """Create a new VM interactively or with options."""


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

        # Check if we have enough arguments for non-interactive mode
        has_required_args = all([name, iso_storage, iso, os_type])

        if has_required_args:
            # Non-interactive mode with arguments
            config: dict[str, Any] = {}

            # Helper function to run async code
            async def get_data():
                async with ProxmoxClient(profile_config) as client:
                    return {
                        "next_vmid": await client.get_next_vmid(),
                        "storages": await client.get_storage_list(node),
                    }

            data = asyncio.run(get_data())

            # VMID
            if vmid is None:
                config["vmid"] = data["next_vmid"]
            else:
                config["vmid"] = vmid

            # Required parameters
            config["name"] = name

            # Optional basic parameters
            if pool:
                config["pool"] = pool
            config["onboot"] = 1 if onboot else 0
            config["agent"] = 1 if agent else 0

            # ISO configuration
            config["ide2"] = f"{iso_storage}:iso/{iso},media=cdrom"

            # OS Type determination
            is_windows = os_type.lower() == "windows"

            if is_windows:
                # Windows OS type mapping
                if not os_version:
                    os_version = "11/2022/2025"  # Default

                if "11" in os_version or "2022" in os_version or "2025" in os_version:
                    config["ostype"] = "win11"
                    needs_tpm = True
                elif "10" in os_version or "2016" in os_version or "2019" in os_version:
                    config["ostype"] = "win10"
                    needs_tpm = False
                elif "8" in os_version or "2012" in os_version:
                    config["ostype"] = "win8"
                    needs_tpm = False
                elif "7" in os_version or "2008" in os_version:
                    config["ostype"] = "win7"
                    needs_tpm = False
                elif "xp" in os_version.lower() or "2003" in os_version:
                    config["ostype"] = "wxp"
                    needs_tpm = False
                elif "2000" in os_version:
                    config["ostype"] = "w2k"
                    needs_tpm = False
                else:
                    config["ostype"] = "win11"
                    needs_tpm = True

                # VirtIO drivers
                if virtio_iso_storage and virtio_iso:
                    config["ide3"] = f"{virtio_iso_storage}:iso/{virtio_iso},media=cdrom"

                # TPM for Windows 11/2022/2025
                if needs_tpm:
                    if not tpm_storage:
                        print_error("--tpm-storage is required for Windows 11/2022/2025")
                        raise typer.Exit(1)
                    if not efi_storage:
                        print_error("--efi-storage is required for Windows 11/2022/2025")
                        raise typer.Exit(1)
                    config["tpmstate0"] = f"{tpm_storage}:1,version=v2.0"
                    config["efidisk0"] = f"{efi_storage}:1,efitype=4m,pre-enrolled-keys=1"
                    config["bios"] = "ovmf"
            else:
                # Linux OS type
                if os_version and "2.4" in os_version:
                    config["ostype"] = "l24"
                else:
                    config["ostype"] = "l26"

            # CPU configuration
            config["sockets"] = sockets if sockets else 1
            config["cores"] = cores if cores else 2

            total_possible_vcpus = config["sockets"] * config["cores"]
            if vcpus and vcpus != total_possible_vcpus:
                if vcpus > total_possible_vcpus:
                    print_warning(f"vCPU count cannot exceed {total_possible_vcpus}, setting to {total_possible_vcpus}")
                    vcpus = total_possible_vcpus
                config["vcpus"] = vcpus

            config["cpu"] = cpu_type if cpu_type else "x86-64-v2-AES"

            # Memory configuration
            memory_value = memory if memory else 2048
            config["memory"] = memory_value
            config["balloon"] = memory_value

            # Disk configuration
            if disk_storage and disk_size:
                format_str = disk_format if disk_format else "qcow2"
                if is_windows:
                    config["scsi0"] = f"{disk_storage}:{disk_size},format={format_str}"
                    config["scsihw"] = "virtio-scsi-pci"
                else:
                    config["virtio0"] = f"{disk_storage}:{disk_size},format={format_str}"

            # Network configuration
            if bridge:
                net_config = f"virtio,bridge={bridge}"
                if vlan:
                    net_config += f",tag={vlan}"
                if firewall:
                    net_config += ",firewall=1"
                if link_down:
                    net_config += ",link_down=1"
                config["net0"] = net_config

            # Create VM
            async def create():
                async with ProxmoxClient(profile_config) as client:
                    vm_id = config.pop("vmid")
                    upid = await client.create_vm(node, vm_id, **config)
                    console.print(f"\n[cyan]Creating VM {vm_id}...[/cyan]")
                    await client.wait_for_task(node, upid, timeout=300)
                    return vm_id

            created_vmid = asyncio.run(create())
            print_success(f"VM {created_vmid} created successfully!")
            return

        # Interactive mode (original code)
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

        console.print("\n[bold cyan]═══ VM Creation Wizard ═══[/bold cyan]\n")

        # 1. VMID
        if vmid is not None:
            config["vmid"] = vmid
        else:
            default_vmid = data["next_vmid"]
            vmid_input = None
            while vmid_input is None:
                try:
                    vmid_str = Prompt.ask(
                        "[bold]VMID[/bold]",
                        default=str(default_vmid),
                    )
                    config["vmid"] = int(vmid_str)
                    vmid_input = True
                except ValueError:
                    print_error("VMID must be a valid number (e.g., 100, 102)")

        # 2. Name
        if name:
            config["name"] = name
        else:
            vm_name = ""
            while not vm_name or not vm_name.strip():
                vm_name = Prompt.ask("[bold]VM Name[/bold]")
                if not vm_name or not vm_name.strip():
                    print_error("VM name cannot be empty")
            config["name"] = vm_name.strip()

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

        # 5. OS Selection - already handles provided arguments with if/else

        # 5. OS Selection
        console.print("\n[bold cyan]─── OS Configuration ───[/bold cyan]\n")

        # 5.1 & 5.2. Storage and ISO selection
        if iso_storage and iso:
            # Use provided ISO configuration
            config["ide2"] = f"{iso_storage}:iso/{iso},media=cdrom"
            selected_storage = iso_storage
        else:
            iso_storages = [s for s in data["storages"] if "iso" in s.get("content", "").split(",")]

            if not iso_storages:
                print_error("No storage with ISO content found")
                raise typer.Exit(1)

            storage_names = [s.get("storage", "") for s in iso_storages]

            if iso_storage:
                # Storage provided but not ISO
                selected_storage = iso_storage
            else:
                # Ask for storage
                console.print("[bold]ISO Storage:[/bold]")
                storage_idx = select_menu(storage_names, "Select storage for ISO:")
                if storage_idx is None:
                    print_error("No storage selected")
                    raise typer.Exit(1)
                selected_storage = storage_names[storage_idx]

            # Get ISOs from selected storage
            async def get_isos():
                async with ProxmoxClient(profile_config) as client:
                    return await client.get_storage_content(node, selected_storage, "iso")

            isos = asyncio.run(get_isos())

            if not isos:
                print_error(f"No ISOs found in storage {selected_storage}")
                raise typer.Exit(1)

            iso_names = [iso.get("volid", "").split("/")[-1] for iso in isos]
            console.print(f"\n[bold]ISO from {selected_storage}:[/bold]")
            iso_idx = select_menu(iso_names, "Select ISO:")
            if iso_idx is None:
                print_error("No ISO selected")
                raise typer.Exit(1)

            selected_iso = isos[iso_idx].get("volid", "")
            config["ide2"] = f"{selected_iso},media=cdrom"

        # 5.3. OS Type
        if os_type:
            is_windows = os_type.lower() == "windows"
        else:
            console.print("\n[bold]OS Type:[/bold]")
            os_types = ["Linux", "Windows"]
            os_idx = select_menu(os_types, "Select OS type:")
            is_windows = os_idx == 1

        # 5.4. OS Version
        if is_windows:
            if os_version:
                # Use provided version
                # Determine ostype based on provided version
                if "11" in os_version or "2022" in os_version or "2025" in os_version:
                    config["ostype"] = "win11"
                    needs_tpm = True
                elif "10" in os_version or "2016" in os_version or "2019" in os_version:
                    config["ostype"] = "win10"
                    needs_tpm = False
                elif "8" in os_version or "2012" in os_version:
                    config["ostype"] = "win8"
                    needs_tpm = False
                elif "7" in os_version or "2008" in os_version:
                    config["ostype"] = "win7"
                    needs_tpm = False
                elif "xp" in os_version.lower() or "2003" in os_version:
                    config["ostype"] = "wxp"
                    needs_tpm = False
                elif "2000" in os_version:
                    config["ostype"] = "w2k"
                    needs_tpm = False
                else:
                    config["ostype"] = "win11"
                    needs_tpm = True
            else:
                # Ask user for version
                win_versions = [
                    "11/2022/2025",
                    "10/2016/2019",
                    "8.x/2012/2012r2",
                    "7/2008r2",
                    "Vista/2008",
                    "XP/2003",
                    "2000",
                ]
                console.print("\n[bold]Windows Version:[/bold]")
                win_idx = select_menu(win_versions, "Select version:")
                # Determine ostype based on selection
                if win_idx == 0:  # 11/2022/2025
                    config["ostype"] = "win11"
                    needs_tpm = True
                elif win_idx == 1:  # 10/2016/2019
                    config["ostype"] = "win10"
                    needs_tpm = False
                elif win_idx == 2:  # 8.x/2012/2012r2
                    config["ostype"] = "win8"
                    needs_tpm = False
                elif win_idx == 3:  # 7/2008r2
                    config["ostype"] = "win7"
                    needs_tpm = False
                elif win_idx == 4:  # Vista/2008
                    config["ostype"] = "win7"
                    needs_tpm = False
                elif win_idx == 5:  # XP/2003
                    config["ostype"] = "wxp"
                    needs_tpm = False
                else:  # 2000
                    config["ostype"] = "w2k"
                    needs_tpm = False

            # 6.1. VirtIO Drivers
            if virtio_iso_storage and virtio_iso:
                # Use provided VirtIO ISO
                config["ide3"] = f"{virtio_iso_storage}:iso/{virtio_iso},media=cdrom"
            elif not virtio_iso and Confirm.ask("\n[bold]Mount VirtIO drivers ISO?[/bold]", default=True):
                # Ask for storage again for VirtIO ISO
                console.print("[bold]VirtIO ISO Storage:[/bold]")
                virtio_storage_idx = select_menu(storage_names, "Select storage for VirtIO ISO:")
                if virtio_storage_idx is not None:
                    virtio_selected_storage = storage_names[virtio_storage_idx]

                    # Get all ISOs from selected storage
                    async def get_virtio_isos():
                        async with ProxmoxClient(profile_config) as client:
                            return await client.get_storage_content(node, virtio_selected_storage, "iso")

                    virtio_isos_all = asyncio.run(get_virtio_isos())

                    if virtio_isos_all:
                        virtio_iso_names = [iso.get("volid", "").split("/")[-1] for iso in virtio_isos_all]
                        console.print(f"\n[bold]VirtIO ISO from {virtio_selected_storage}:[/bold]")
                        virtio_idx = select_menu(virtio_iso_names, "Select VirtIO ISO:")
                        if virtio_idx is not None:
                            virtio_iso = virtio_isos_all[virtio_idx].get("volid", "")
                            config["ide3"] = f"{virtio_iso},media=cdrom"
                    else:
                        print_warning(f"No ISOs found in storage {virtio_selected_storage}")

            # 6.3. TPM
            if needs_tpm:
                console.print("\n[bold cyan]TPM required for this OS[/bold cyan]")
                storage_names_all = [s.get("storage", "") for s in data["storages"]]
                console.print("[bold]TPM Storage:[/bold]")
                tpm_idx = select_menu(storage_names_all, "Select storage for TPM:")
                if tpm_idx is not None:
                    tpm_storage = storage_names_all[tpm_idx]
                    config["tpmstate0"] = f"{tpm_storage}:1,version=v2.0"

            # 6.4. EFI Disk
            if needs_tpm:
                storage_names_all = [s.get("storage", "") for s in data["storages"]]
                console.print("[bold]EFI Storage:[/bold]")
                efi_idx = select_menu(storage_names_all, "Select storage for EFI:")
                if efi_idx is not None:
                    efi_storage = storage_names_all[efi_idx]
                    config["efidisk0"] = f"{efi_storage}:1,efitype=4m,pre-enrolled-keys=1"
                    config["bios"] = "ovmf"

        else:
            # Linux
            linux_versions = [
                "6.x Kernel or 2.6 Kernel",
                "2.4 Kernel",
            ]
            console.print("\n[bold]Linux Kernel Version:[/bold]")
            linux_idx = select_menu(linux_versions, "Select kernel version:")

            # Determine ostype based on kernel version
            if linux_idx == 0:  # 6.x or 2.6 Kernel
                config["ostype"] = "l26"
            else:  # 2.4 Kernel
                config["ostype"] = "l24"

        # 7. QEMU Guest Agent
        if agent is not None:
            config["agent"] = 1 if agent else 0
        else:
            console.print("\n[bold cyan]─── Additional Configuration ───[/bold cyan]\n")
            config["agent"] = 1 if Confirm.ask("[bold]Enable QEMU Guest Agent?[/bold]", default=True) else 0

        # 8. CPU
        if sockets or cores or cpu_type:
            # At least one CPU parameter provided
            config["sockets"] = sockets if sockets else 1
            config["cores"] = cores if cores else 2
        else:
            console.print("\n[bold]CPU Configuration:[/bold]")
            config["sockets"] = IntPrompt.ask("Number of sockets", default=1)
            config["cores"] = IntPrompt.ask("Number of cores per socket", default=2)

        # Calculate total possible vCPUs
        total_possible_vcpus = config["sockets"] * config["cores"]

        # Ask for vCPU count at startup (hot-plug)
        if vcpus:
            if vcpus > total_possible_vcpus:
                print_warning(f"vCPU count cannot exceed {total_possible_vcpus}, setting to {total_possible_vcpus}")
                config["vcpus"] = total_possible_vcpus
            else:
                config["vcpus"] = vcpus
        elif vcpus is None:
            # Ask interactively
            console.print(f"\n[dim]Total vCPUs available: {total_possible_vcpus}[/dim]")
            vcpu_count = IntPrompt.ask(
                "vCPU count at startup (leave empty to use all)",
                default=total_possible_vcpus
            )
            if vcpu_count and vcpu_count != total_possible_vcpus:
                if vcpu_count > total_possible_vcpus:
                    print_warning(f"vCPU count cannot exceed {total_possible_vcpus}, setting to {total_possible_vcpus}")
                    vcpu_count = total_possible_vcpus
                config["vcpus"] = vcpu_count

        if cpu_type:
            config["cpu"] = cpu_type
        elif cpu_type is None:
            console.print("\n[bold]CPU Type:[/bold]")
            cpu_types = ["x86-64-v2-AES (default)", "host"]
            cpu_idx = select_menu(cpu_types, "Select CPU type:")
            if cpu_idx == 1:
                config["cpu"] = "host"
            else:
                config["cpu"] = "x86-64-v2-AES"

        # 9. RAM
        if memory:
            config["memory"] = memory
            config["balloon"] = memory
        elif memory is None:
            console.print("\n[bold]Memory Configuration:[/bold]")
            memory_value = IntPrompt.ask("RAM (MiB)", default=2048)
            config["memory"] = memory_value
            # Set balloon (minimum memory) to the same value as memory
            # In Proxmox, balloon parameter represents minimum memory in MiB
            config["balloon"] = memory_value

        # 9.5. Primary Disk
        if disk_storage and disk_size:
            # Use provided disk configuration
            format_str = disk_format if disk_format else "qcow2"
            if is_windows:
                config["scsi0"] = f"{disk_storage}:{disk_size},format={format_str}"
                config["scsihw"] = "virtio-scsi-pci"
            else:
                config["virtio0"] = f"{disk_storage}:{disk_size},format={format_str}"
        elif disk_storage is None and disk_size is None:
            # Ask interactively
            console.print("\n[bold cyan]─── Disk Configuration ───[/bold cyan]\n")
            if Confirm.ask("[bold]Add primary disk?[/bold]", default=True):
                storage_names_all = [s.get("storage", "") for s in data["storages"]]
                console.print("[bold]Disk Storage:[/bold]")
                disk_idx = select_menu(storage_names_all, "Select storage for primary disk:")
                if disk_idx is not None:
                    disk_storage = storage_names_all[disk_idx]
                    disk_size = IntPrompt.ask("Disk size (GB)", default=32)

                    # Disk format
                    console.print("\n[bold]Disk Format:[/bold]")
                    disk_formats = ["qcow2", "raw", "vmdk"]
                    format_idx = select_menu(disk_formats, "Select disk format:")
                    disk_format = disk_formats[format_idx] if format_idx is not None else "qcow2"

                    # Use virtio for Linux, scsi for Windows
                    if is_windows:
                        config["scsi0"] = f"{disk_storage}:{disk_size},format={disk_format}"
                        config["scsihw"] = "virtio-scsi-pci"
                    else:
                        config["virtio0"] = f"{disk_storage}:{disk_size},format={disk_format}"

        # 10. Network
        if bridge:
            # Use provided network configuration
            net_config = f"virtio,bridge={bridge}"

            # VLAN
            if vlan:
                net_config += f",tag={vlan}"

            # Firewall
            if firewall:
                net_config += ",firewall=1"

            # Link state
            if link_down:
                net_config += ",link_down=1"

            config["net0"] = net_config
        elif bridge is None:
            # Ask interactively
            console.print("\n[bold cyan]─── Network Configuration ───[/bold cyan]\n")
            bridges = [b for b in data["bridges"] if b.get("type") == "bridge"]

            if bridges:
                bridge_names = [b.get("iface", "") for b in bridges]
                console.print("[bold]Bridge:[/bold]")
                bridge_idx = select_menu(bridge_names, "Select bridge:")
                if bridge_idx is not None:
                    bridge = bridge_names[bridge_idx]

                    # Build net0 config
                    net_config = f"virtio,bridge={bridge}"

                    # VLAN
                    vlan = Prompt.ask("VLAN tag (leave empty for none)", default="")
                    if vlan:
                        net_config += f",tag={vlan}"

                    # Firewall
                    if Confirm.ask("Enable firewall?", default=False):
                        net_config += ",firewall=1"

                    # Link state
                    if Confirm.ask("Start disconnected?", default=False):
                        net_config += ",link_down=1"

                    config["net0"] = net_config

        # Summary
        console.print("\n[bold cyan]═══ Configuration Summary ═══[/bold cyan]\n")
        console.print(f"[bold]VMID:[/bold] {config['vmid']}")
        console.print(f"[bold]Name:[/bold] {config['name']}")
        if "pool" in config:
            console.print(f"[bold]Pool:[/bold] {config['pool']}")
        if "tags" in config:
            console.print(f"[bold]Tags:[/bold] {config['tags']}")
        console.print(f"[bold]CPU:[/bold] {config['sockets']} socket(s) × {config['cores']} core(s) ({config['cpu']})")
        console.print(f"[bold]Memory:[/bold] {config['memory']} MiB")
        if "net0" in config:
            console.print(f"[bold]Network:[/bold] {config['net0']}")
        console.print(f"[bold]OS Type:[/bold] {config['ostype']}")
        if "ide2" in config:
            console.print(f"[bold]ISO:[/bold] {config['ide2']}")

        console.print()

        if not Confirm.ask("[bold]Create VM with this configuration?[/bold]", default=True):
            print_cancelled()
            return

        # Create VM
        async def create():
            async with ProxmoxClient(profile_config) as client:
                vmid = config.pop("vmid")
                upid = await client.create_vm(node, vmid, **config)
                console.print(f"\n[cyan]Creating VM...[/cyan]")
                console.print(f"[cyan]Task ID:[/cyan] {upid}")
                await client.wait_for_task(node, upid, timeout=300)
                return vmid

        created_vmid = asyncio.run(create())

        print_success(f"VM {created_vmid} created successfully!")

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    except KeyboardInterrupt:
        console.print()
        print_cancelled()
        raise typer.Exit(0)


@app.command("vnc")
@async_to_sync
async def vm_vnc(
    vmid: int = typer.Argument(None, help="VM ID"),
    background: bool = typer.Option(False, "--background", "-b", is_flag=True, help="Run VNC server in background"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Open an authenticated VNC console for a VM."""
    from ..utils import open_browser_window
    from ..utils.network import find_free_port
    from ..vnc.server import VNCProxyServer

    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if vmid is None:
                vmid = await _select_vm(client)
                if vmid is None:
                    print_cancelled()
                    return
            vms = await client.get_vms()
            vm = next((v for v in vms if v.get("vmid") == vmid), None)

            if not vm:
                print_error(f"VM {vmid} not found")
                raise typer.Exit(1)

            node = vm.get("node")
            vm_name = vm.get("name", "").strip()
            vm_status = vm.get("status", "unknown")

            if vm_status != "running":
                print_error(
                    f"VM {vmid} ({vm_name}) is not running (status: {vm_status}). "
                    "Start the VM before opening a VNC console."
                )
                raise typer.Exit(1)

            vnc_data = await client.create_vm_vncproxy(
                node, vmid, websocket=True, generate_password=True
            )

            host = resolve_node_host(profile_config)

            server_config = {
                "proxmox_host": host,
                "proxmox_port": profile_config.port,
                "ws_path": f"/api2/json/nodes/{node}/qemu/{vmid}/vncwebsocket",
                "vncticket": vnc_data["ticket"],
                "pve_port": int(vnc_data["port"]),
                "auth_headers": dict(client._headers),
                "local_port": find_free_port(),
                "verify_ssl": profile_config.verify_ssl,
                "vnc_password": vnc_data.get("password"),
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
            print_success(f"VNC console for VM {vmid} ({vm_name}) running in background (PID: {proc.pid})")
        else:
            print_success(f"Opening VNC console for VM {vmid} ({vm_name})...")
            console.print("[dim]Press Enter to stop the server[/dim]")
            await server.run()

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("ssh")
@async_to_sync
async def vm_ssh(
    vmid: int = typer.Argument(None, help="VM ID"),
    user: str = typer.Option(None, "--user", "-u", help="SSH user"),
    port: int = typer.Option(None, "--port", "-P", help="SSH port"),
    key: str = typer.Option(None, "--key", "-i", help="Path to SSH key"),
    jump: bool = typer.Option(False, "--jump", "-j", is_flag=True, help="Use node as jump host"),
    command: str = typer.Option(None, "--command", "-c", help="Execute command instead of shell"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """SSH into a VM (IP resolved via QEMU Guest Agent)."""
    from ..ssh import build_ssh_command, exec_ssh
    from ..utils.network import resolve_vm_ip

    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if vmid is None:
                vmid = await _select_vm(client)
                if vmid is None:
                    print_cancelled()
                    return
            vms = await client.get_vms()
            vm = next((v for v in vms if v.get("vmid") == vmid), None)

            if not vm:
                print_error(f"VM {vmid} not found")
                raise typer.Exit(1)

            if vm.get("status") != "running":
                print_error(f"VM {vmid} is not running")
                raise typer.Exit(1)

            node = vm.get("node")
            ip = await resolve_vm_ip(client, node, vmid)

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


@app.command("rdp")
@async_to_sync
async def vm_rdp(
    vmid: int = typer.Argument(None, help="VM ID"),
    user: str = typer.Option(None, "--user", "-u", help="RDP user"),
    domain: str = typer.Option(None, "--domain", "-d", help="RDP domain"),
    port: int = typer.Option(None, "--port", "-P", help="RDP port"),
    fullscreen: bool = typer.Option(False, "--fullscreen", "-f", is_flag=True, help="Fullscreen mode"),
    resolution: str = typer.Option(None, "--resolution", "-r", help="Resolution (e.g. 1920x1080)"),
    jump: bool = typer.Option(False, "--jump", "-j", is_flag=True, help="Use node as jump host for RDP"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """RDP into a VM (IP resolved via QEMU Guest Agent)."""
    from ..rdp import (
        build_rdp_command,
        create_ssh_tunnel,
        detect_rdp_client,
        exec_rdp,
        get_install_hint,
    )
    from ..utils.network import find_free_port, resolve_vm_ip

    # Detect RDP client first
    client_type, client_path = detect_rdp_client()
    if not client_type:
        print_error(f"No RDP client found. {get_install_hint()}")
        raise typer.Exit(1)

    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if vmid is None:
                vmid = await _select_vm(client)
                if vmid is None:
                    print_cancelled()
                    return
            vms = await client.get_vms()
            vm = next((v for v in vms if v.get("vmid") == vmid), None)

            if not vm:
                print_error(f"VM {vmid} not found")
                raise typer.Exit(1)

            if vm.get("status") != "running":
                print_error(f"VM {vmid} is not running")
                raise typer.Exit(1)

            node = vm.get("node")

            # Warn if VM appears to be Linux
            osinfo = await client.get_vm_osinfo(node, vmid)
            os_id = osinfo.get("id", "")
            if os_id and os_id != "mswindows":
                print_warning(
                    f"VM appears to be Linux ({osinfo.get('pretty-name', os_id)}). "
                    f"RDP might not be available. Consider 'pvecli vm ssh {vmid}'."
                )

            ip = await resolve_vm_ip(client, node, vmid)

        rdp_user = user or profile_config.rdp_user
        rdp_port = port or profile_config.rdp_port

        # Prompt for credentials
        from getpass import getpass
        rdp_password = getpass("Password: ")
        rdp_domain = domain if domain is not None else (prompt("Domain (empty for none)") or "")

        tunnel_proc = None
        rdp_host = ip

        if jump:
            node_host = resolve_node_host(profile_config)
            ssh_user = profile_config.ssh_user or "root"
            ssh_port = profile_config.ssh_port
            ssh_key = profile_config.ssh_key
            local_port = find_free_port()

            console.print(f"[dim]Creating SSH tunnel via {node_host}...[/dim]")
            tunnel_proc = create_ssh_tunnel(
                node_host, ssh_user, ssh_port, ssh_key, ip, rdp_port, local_port,
            )
            # Wait briefly for tunnel to establish
            time.sleep(1)

            if tunnel_proc.poll() is not None:
                print_error("SSH tunnel failed to start")
                raise typer.Exit(1)

            rdp_host = "localhost"
            rdp_port = local_port

        try:
            args = build_rdp_command(client_type, rdp_host, rdp_port, rdp_user, rdp_password, rdp_domain, fullscreen, resolution)
            target = f"{rdp_user}@{rdp_host}" if rdp_user else rdp_host
            console.print(f"[dim]Connecting to {target}:{rdp_port} via {client_type}...[/dim]")
            exec_rdp(args)
        finally:
            if tunnel_proc:
                tunnel_proc.terminate()

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
