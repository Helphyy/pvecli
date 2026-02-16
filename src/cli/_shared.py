"""Shared command implementations for VM and CT modules.

This module provides parameterized implementations for commands that
are nearly identical between VM and CT (tags, snapshots, VNC, SSH, list).
Each function takes callbacks/labels so it works for both resource types.
"""

import asyncio
import time
from typing import Any, Callable, Coroutine

import typer
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ..api.client import ProxmoxClient
from ..utils import (
    confirm,
    console,
    print_cancelled,
    print_error,
    print_info,
    print_success,
    print_warning,
    prompt,
)
from ..utils.menu import multi_select_menu, select_menu
from ..utils.network import resolve_node_host
from .tag import _parse_color_map


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def pick_node(client: ProxmoxClient) -> str | None:
    """Interactive single-select for a node. Returns node name or None."""
    nodes = await client.get_nodes()
    node_names = sorted(n.get("node", "") for n in nodes if n.get("node"))
    if not node_names:
        print_info("No nodes found")
        return None
    idx = select_menu(node_names, "  Select node:")
    if idx is None:
        print_cancelled()
        return None
    return node_names[idx]


def parse_kv(config_str: str) -> dict:
    """Parse comma-separated key=value string into ordered dict."""
    result = {}
    for part in config_str.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            result[k] = v
        else:
            result[part] = ""
    return result


def build_kv(params: dict) -> str:
    """Rebuild comma-separated key=value string from dict."""
    parts = []
    for k, v in params.items():
        parts.append(f"{k}={v}" if v else k)
    return ",".join(parts)


def extract_size(config_str: str) -> str:
    """Extract size= value from a disk config string."""
    for part in config_str.split(","):
        if part.startswith("size="):
            return part[5:]
    return ""


def parse_id_list(raw: str, label: str = "VM") -> list[int]:
    """Parse comma-separated ID string into list of ints.

    Args:
        raw: Comma-separated string (e.g. "100,101,102").
        label: Resource label for error messages ("VM" or "CT").
    """
    result = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            print_error(f"Invalid {label} ID: '{part}'")
            raise typer.Exit(1)
    if not result:
        print_error(f"No valid {label} IDs provided")
        raise typer.Exit(1)
    return result


async def validate_resources(
    client: ProxmoxClient,
    id_list: list[int],
    resource_type: str,
    label: str,
) -> list[dict[str, Any]]:
    """Validate a list of VM/CT IDs and return their info.

    Args:
        client: ProxmoxClient instance.
        id_list: List of VMID/CTID ints.
        resource_type: "qemu" or "lxc".
        label: "VM" or "Container" for messages.

    Returns:
        List of dicts with keys: id, node, status.
    """
    resources = await client.get_cluster_resources(resource_type="vm")
    result = []
    for rid in id_list:
        resource = next(
            (r for r in resources if r.get("vmid") == rid and r.get("type") == resource_type),
            None,
        )
        if not resource:
            print_error(f"{label} {rid} not found")
            raise typer.Exit(1)
        result.append({"id": rid, "node": resource.get("node"), "status": resource.get("status", "unknown")})
    return result


def confirm_action(
    id_list: list[int],
    action: str,
    label: str,
    yes: bool,
) -> bool:
    """Show confirmation prompt for a multi-target action.

    Args:
        id_list: List of VMID/CTID ints.
        action: Action text (e.g. "Hard stop", "Shutdown").
        label: "VM" or "container".
        yes: Skip confirmation if True.

    Returns:
        True if confirmed, False otherwise.
    """
    if yes:
        return True
    if len(id_list) == 1:
        msg = f"{action} {label} {id_list[0]}?"
    else:
        ids = ", ".join(str(v) for v in id_list)
        msg = f"{action} {len(id_list)} {label}s ({ids})?"
    if not confirm(msg, default=False):
        print_cancelled()
        return False
    return True


async def run_with_spinner(
    client: ProxmoxClient,
    node: str,
    action_desc: str,
    coro: Coroutine,
    wait_desc: str | None = None,
    timeout: int | None = None,
) -> str:
    """Run an API action with a Progress spinner and wait for completion.

    Args:
        client: ProxmoxClient instance.
        node: Node name.
        action_desc: Initial spinner description (e.g. "Starting VM 100...").
        coro: Coroutine that returns a UPID string.
        wait_desc: Spinner text while waiting (e.g. "Waiting for VM 100 to start...").
        timeout: Optional timeout for wait_for_task.

    Returns:
        The UPID string.
    """
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress.add_task(description=action_desc, total=None)
        upid = await coro
        if wait_desc:
            progress.update(0, description=wait_desc)
            kwargs = {"timeout": timeout} if timeout else {}
            await client.wait_for_task(node, upid, **kwargs)
    return upid


# ---------------------------------------------------------------------------
# Tag commands
# ---------------------------------------------------------------------------

async def shared_list_tags(
    client: ProxmoxClient,
    resource_id: int,
    label: str,
    get_config: Callable[..., Coroutine],
    node: str,
) -> None:
    """List tags for a VM or container.

    Args:
        client: ProxmoxClient instance.
        resource_id: VMID or CTID.
        label: "VM" or "CT".
        get_config: Async callable returning config dict.
        node: Node name.
    """
    config = await get_config()
    tags = config.get("tags", "")

    cluster_opts = await client.get_cluster_options()
    color_map = _parse_color_map(cluster_opts.get("tag-style", ""))

    if tags:
        tag_list = [tag.strip() for tag in tags.split(";") if tag.strip()]
        print_info(f"Tags for {label} {resource_id}:")
        for tag in tag_list:
            color = color_map.get(tag, "")
            if color:
                parts = color.split(":")
                bg = parts[0]
                fg = parts[1] if len(parts) > 1 else "FFFFFF"
                console.print(f"  [on #{bg}][#{fg}] {tag} [/]")
            else:
                console.print(f"  - {tag}")
    else:
        print_info(f"No tags found for {label} {resource_id}")


async def shared_add_tag(
    client: ProxmoxClient,
    resource_id: int,
    label: str,
    node: str,
    tags_arg: str | None,
    replace: bool,
    get_config: Callable[..., Coroutine],
    update_config: Callable[..., Coroutine],
) -> None:
    """Add tags to a VM or container.

    Args:
        client: ProxmoxClient instance.
        resource_id: VMID or CTID.
        label: "VM" or "CT".
        node: Node name.
        tags_arg: Comma-separated tags string, or None for interactive.
        replace: Whether to replace all existing tags.
        get_config: Async callable returning config dict.
        update_config: Async callable accepting tags=str keyword arg.
    """
    config = await get_config()
    current_tags = config.get("tags", "")

    if tags_arg is None:
        # Interactive mode: show menu with existing cluster tags
    

        all_resources = await client.get_cluster_resources(resource_type="vm")
        known_tags: set[str] = set()
        for r in all_resources:
            for t in r.get("tags", "").split(";"):
                t = t.strip()
                if t:
                    known_tags.add(t)
        cluster_opts = await client.get_cluster_options()
        cm = _parse_color_map(cluster_opts.get("tag-style", ""))
        known_tags.update(cm)

        if not known_tags:
            print_error("No tags found in the cluster")
            raise typer.Exit(1)

        current_tag_list = [t.strip() for t in current_tags.split(";") if t.strip()]
        sorted_tags = sorted(known_tags)
        preselected = [i for i, t in enumerate(sorted_tags) if t in current_tag_list]

        entries = sorted_tags + ["+ Add custom tag"]
        sel = multi_select_menu(entries, "  Tags (Space to toggle, Enter to confirm):", preselected=preselected)
        if sel is None:
            print_cancelled()
            return
        chosen = [entries[i] for i in sel]
        if "+ Add custom tag" in chosen:
            custom = prompt("  Custom tag name")
            if custom.strip():
                chosen = [t for t in chosen if t != "+ Add custom tag"] + [custom.strip()]
            else:
                chosen = [t for t in chosen if t != "+ Add custom tag"]
        input_tag_list = chosen
        if not input_tag_list:
            print_cancelled()
            return
    else:
        input_tag_list = [t.strip() for t in tags_arg.split(",") if t.strip()]

    if not input_tag_list:
        print_error("No valid tags provided")
        raise typer.Exit(1)

    if not replace and current_tags:
        tag_list = [t.strip() for t in current_tags.split(";") if t.strip()]
        added_tags = []
        skipped_tags = []
        for new_tag in input_tag_list:
            if new_tag in tag_list:
                skipped_tags.append(new_tag)
            else:
                tag_list.append(new_tag)
                added_tags.append(new_tag)

        new_tags = ";".join(tag_list)

        if skipped_tags:
            for skipped in skipped_tags:
                print_warning(f"Tag '{skipped}' already exists on {label} {resource_id}")

        if not added_tags:
            print_info("No new tags to add")
            return
    else:
        new_tags = ";".join(input_tag_list)
        added_tags = input_tag_list

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        tag_desc = ", ".join(added_tags)
        action = "Replacing" if replace else "Adding"
        progress.add_task(description=f"{action} tag(s) '{tag_desc}' on {label} {resource_id}...", total=None)
        await update_config(tags=new_tags)

    if replace:
        if len(added_tags) == 1:
            print_success(f"Tags replaced with '{added_tags[0]}' on {label} {resource_id}")
        else:
            print_success(f"Tags replaced with '{', '.join(added_tags)}' on {label} {resource_id}")
    else:
        if len(added_tags) == 1:
            print_success(f"Tag '{added_tags[0]}' added to {label} {resource_id}")
        else:
            print_success(f"Tags '{', '.join(added_tags)}' added to {label} {resource_id}")


async def shared_remove_tag(
    client: ProxmoxClient,
    resource_id: int,
    label: str,
    node: str,
    tags_arg: str | None,
    get_config: Callable[..., Coroutine],
    update_config: Callable[..., Coroutine],
) -> None:
    """Remove tags from a VM or container."""
    config = await get_config()
    current_tags = config.get("tags", "")

    if not current_tags:
        print_warning(f"{label.capitalize()} {resource_id} has no tags")
        return

    tag_list = [t.strip() for t in current_tags.split(";") if t.strip()]

    if tags_arg is None:
        sel = multi_select_menu(tag_list, "  Tags to remove (Space to toggle, Enter to confirm):")
        if sel is None:
            print_cancelled()
            return
        input_tag_list = [tag_list[i] for i in sel]
        if not input_tag_list:
            print_cancelled()
            return
    else:
        input_tag_list = [t.strip() for t in tags_arg.split(",") if t.strip()]

    if not input_tag_list:
        print_error("No valid tags provided")
        raise typer.Exit(1)

    removed_tags = []
    not_found_tags = []
    for tag_to_remove in input_tag_list:
        if tag_to_remove in tag_list:
            tag_list.remove(tag_to_remove)
            removed_tags.append(tag_to_remove)
        else:
            not_found_tags.append(tag_to_remove)

    if not_found_tags:
        for not_found in not_found_tags:
            print_warning(f"Tag '{not_found}' not found on {label} {resource_id}")

    if not removed_tags:
        print_info("No tags to remove")
        return

    new_tags = ";".join(tag_list) if tag_list else ""

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        tag_desc = ", ".join(removed_tags)
        progress.add_task(description=f"Removing tag(s) '{tag_desc}' from {label} {resource_id}...", total=None)
        await update_config(tags=new_tags)

    if len(removed_tags) == 1:
        print_success(f"Tag '{removed_tags[0]}' removed from {label} {resource_id}")
    else:
        print_success(f"Tags '{', '.join(removed_tags)}' removed from {label} {resource_id}")


# ---------------------------------------------------------------------------
# Snapshot commands
# ---------------------------------------------------------------------------

async def shared_list_snapshots(
    client: ProxmoxClient,
    resource_id: int,
    label: str,
    node: str,
    get_snapshots: Callable[..., Coroutine],
    show_vmstate: bool = False,
) -> None:
    """List snapshots for a VM or container."""
    snapshots = await get_snapshots()

    # Filter out 'current' which is not a real snapshot
    snapshots = [s for s in snapshots if s.get("name") != "current"]

    if not snapshots:
        print_info(f"No snapshots found for {label} {resource_id}")
        return

    table = Table(
        title=f"Snapshots for {label} {resource_id}",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Name", style="cyan")
    table.add_column("Description")
    table.add_column("Date")
    if show_vmstate:
        table.add_column("RAM")

    for snap in snapshots:
        name = snap.get("name", "-")
        desc = snap.get("description", "-")
        snaptime = snap.get("snaptime", 0)

        if snaptime:
            from datetime import datetime

            date_str = datetime.fromtimestamp(snaptime).strftime("%Y-%m-%d %H:%M:%S")
        else:
            date_str = "-"

        if show_vmstate:
            vmstate = "Yes" if snap.get("vmstate") else "No"
            table.add_row(name, desc, date_str, vmstate)
        else:
            table.add_row(name, desc, date_str)

    console.print(table)


async def shared_create_snapshot(
    client: ProxmoxClient,
    resource_id: int,
    label: str,
    node: str,
    name: str,
    description: str | None,
    wait: bool,
    create_fn: Callable[..., Coroutine],
    always_wait: bool = False,
) -> None:
    """Create a snapshot for a VM or container."""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress.add_task(
            description=f"Creating snapshot '{name}' for {label} {resource_id}...", total=None
        )
        upid = await create_fn()

        if wait or always_wait:
            progress.update(0, description="Waiting for snapshot to complete...")
            await client.wait_for_task(node, upid, timeout=600)

    print_success(f"Snapshot '{name}' created for {label} {resource_id}")


async def shared_rollback_snapshot(
    client: ProxmoxClient,
    resource_id: int,
    label: str,
    node: str,
    name: str,
    yes: bool,
    wait: bool,
    reboot: bool,
    rollback_fn: Callable[..., Coroutine],
    get_status_fn: Callable[..., Coroutine],
    start_fn: Callable[..., Coroutine],
    reboot_fn: Callable[..., Coroutine],
) -> None:
    """Rollback a VM or container to a snapshot."""
    upid = None

    if not yes:
        if not confirm(
            f"Rollback {label} {resource_id} to snapshot '{name}'? Current state will be lost!",
            default=False,
        ):
            print_cancelled()
            return

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            progress.add_task(
                description=f"Rolling back {label} {resource_id} to snapshot '{name}'...", total=None
            )
            upid = await rollback_fn()

            if wait or reboot:
                progress.update(0, description="Waiting for rollback to complete...")
                await client.wait_for_task(node, upid, timeout=600)

        print_success(f"{label} {resource_id} rolled back to snapshot '{name}'")

        if reboot:
            # Check current status after rollback with timeout
            current_status = None
            start_check = time.time()
            timeout_check = 10

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task(description=f"Checking {label} {resource_id} status...", total=None)

                while time.time() - start_check < timeout_check:
                    try:
                        status_data = await get_status_fn()
                        current_status = status_data.get("status")
                        if current_status:
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(0.5)

            if not current_status:
                print_error(f"Could not determine {label} {resource_id} status after rollback")
                raise typer.Exit(1)

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                if current_status != "running":
                    progress.add_task(description=f"Starting {label} {resource_id}...", total=None)
                    upid = await start_fn()
                    progress.update(0, description=f"Waiting for {label} {resource_id} to start...")
                    await client.wait_for_task(node, upid)
                    print_success(f"{label} {resource_id} started successfully")
                else:
                    progress.add_task(description=f"Rebooting {label} {resource_id}...", total=None)
                    upid = await reboot_fn()
                    progress.update(0, description=f"Waiting for {label} {resource_id} to reboot...")
                    await client.wait_for_task(node, upid)
                    print_success(f"{label} {resource_id} rebooted successfully")

    except (KeyboardInterrupt, asyncio.CancelledError):
        if upid and node:
            print_warning("Stopping task...")
            await client.stop_task(node, upid)
        print_cancelled()
        print_info("Check Proxmox to verify task status")
        raise typer.Exit(1)


async def shared_delete_snapshot(
    client: ProxmoxClient,
    resource_id: int,
    label: str,
    node: str,
    name: str,
    yes: bool,
    wait: bool,
    delete_fn: Callable[..., Coroutine],
) -> None:
    """Delete a snapshot from a VM or container."""
    if not yes:
        if not confirm(f"Delete snapshot '{name}' from {label} {resource_id}?", default=False):
            print_cancelled()
            return

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress.add_task(
            description=f"Deleting snapshot '{name}' from {label} {resource_id}...", total=None
        )
        upid = await delete_fn()

        if wait:
            progress.update(0, description="Waiting for deletion to complete...")
            await client.wait_for_task(node, upid, timeout=600)

    print_success(f"Snapshot '{name}' deleted from {label} {resource_id}")


# ---------------------------------------------------------------------------
# VNC command
# ---------------------------------------------------------------------------

async def shared_vnc(
    client: ProxmoxClient,
    resource_id: int,
    label: str,
    profile_config: Any,
    get_resources: Callable[..., Coroutine],
    create_vncproxy: Callable[..., Coroutine],
    api_type: str,
    generate_password: bool = False,
) -> None:
    """Open a VNC console for a VM or container.

    Args:
        client: ProxmoxClient instance.
        resource_id: VMID or CTID.
        label: "VM" or "CT".
        profile_config: Profile configuration.
        get_resources: Async callable returning list of resources.
        create_vncproxy: Async callable returning VNC proxy data.
        api_type: "qemu" or "lxc".
        generate_password: Whether VNC proxy generates a password.
    """
    from ..utils import open_browser_window
    from ..utils.network import find_free_port
    from ..vnc.server import VNCProxyServer

    resources = await get_resources()
    resource = next((r for r in resources if r.get("vmid") == resource_id), None)

    if not resource:
        print_error(f"{label} {resource_id} not found")
        raise typer.Exit(1)

    node = resource.get("node")
    resource_name = resource.get("name", "").strip()
    resource_status = resource.get("status", "unknown")

    if resource_status != "running":
        print_error(
            f"{label} {resource_id} ({resource_name}) is not running (status: {resource_status}). "
            f"Start the {label.lower()} before opening a VNC console."
        )
        raise typer.Exit(1)

    vnc_data = await create_vncproxy()

    host = resolve_node_host(profile_config)

    vnc_password = vnc_data.get("password") if generate_password else vnc_data["ticket"]

    server_config = {
        "proxmox_host": host,
        "proxmox_port": profile_config.port,
        "ws_path": f"/api2/json/nodes/{node}/{api_type}/{resource_id}/vncwebsocket",
        "vncticket": vnc_data["ticket"],
        "pve_port": int(vnc_data["port"]),
        "auth_headers": dict(client._headers),
        "local_port": find_free_port(),
        "verify_ssl": profile_config.verify_ssl,
        "vnc_password": vnc_password,
    }

    server = VNCProxyServer(**server_config)
    url = server.get_browser_url()
    print_success(f"Opening VNC console for {label} {resource_id} ({resource_name})...")
    console.print("[dim]Press Enter to stop the server[/dim]")
    open_browser_window(url)
    await server.run()


# ---------------------------------------------------------------------------
# SSH command
# ---------------------------------------------------------------------------

async def shared_ssh(
    client: ProxmoxClient,
    resource_id: int,
    label: str,
    profile_config: Any,
    get_resources: Callable[..., Coroutine],
    resolve_ip: Callable[..., Coroutine],
    user: str | None,
    port: int | None,
    key: str | None,
    jump: bool,
    command: str | None,
) -> None:
    """SSH into a VM or container."""
    from ..ssh import build_ssh_command, exec_ssh

    resources = await get_resources()
    resource = next((r for r in resources if r.get("vmid") == resource_id), None)

    if not resource:
        print_error(f"{label} {resource_id} not found")
        raise typer.Exit(1)

    if resource.get("status") != "running":
        print_error(f"{label} {resource_id} is not running")
        raise typer.Exit(1)

    node = resource.get("node")
    ip = await resolve_ip(client, node, resource_id)

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
