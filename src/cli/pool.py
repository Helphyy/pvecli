"""Pool management commands."""

import typer
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ..api.client import ProxmoxClient
from ..api.exceptions import PVECliError
from ..config import ConfigManager
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
from ..utils.helpers import async_to_sync, ordered_group
from ..utils.menu import multi_select_menu, select_menu

app = typer.Typer(help="Manage resource pools", no_args_is_help=True, cls=ordered_group(["add", "remove", "content", "list", "show"]))
content_app = typer.Typer(help="Manage pool members (VMs/CTs)", no_args_is_help=True)
app.add_typer(content_app, name="content")


@app.command("list")
@async_to_sync
async def list_pools(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """List all resource pools."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            pools = await client.get("/pools")

            # Handle response format
            if isinstance(pools, dict) and "data" in pools:
                pools = pools.get("data", [])
            elif not isinstance(pools, list):
                pools = []

            if not pools:
                print_info("No pools found")
                return

            # Sort by poolid
            pools = sorted(pools, key=lambda x: x.get("poolid", ""))

            table = Table(title="Resource Pools", show_header=True, header_style="bold cyan")
            table.add_column("Pool ID", style="cyan")
            table.add_column("Comment")

            for pool in pools:
                poolid = pool.get("poolid", "-")
                comment = pool.get("comment", "")
                table.add_row(poolid, comment)

            console.print(table)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("show")
@async_to_sync
async def show_pool(
    poolid: str = typer.Argument(None, help="Pool ID"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Show detailed information about a pool."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if poolid is None:
                pools = await client.get_pools()
                if not pools:
                    print_info("No pools found")
                    return
                pool_ids = sorted(p.get("poolid", "") for p in pools if p.get("poolid"))
                idx = select_menu(pool_ids, "  Select pool:")
                if idx is None:
                    print_cancelled()
                    return
                poolid = pool_ids[idx]

            pool_data = await client.get(f"/pools/{poolid}")

            # Handle response format
            if isinstance(pool_data, dict) and "data" in pool_data:
                pool_data = pool_data.get("data", {})

            comment = pool_data.get("comment", "")
            members = pool_data.get("members", [])

            lines = []
            lines.append(f"[bold]Pool ID:[/bold]     {poolid}")
            if comment:
                lines.append(f"[bold]Comment:[/bold]     {comment}")

            # Group members by type
            vms = [m for m in members if m.get("type") == "qemu"]
            cts = [m for m in members if m.get("type") == "lxc"]
            storages = [m for m in members if m.get("type") == "storage"]

            if vms:
                lines.append("")
                lines.append(f"[bold]VMs ({len(vms)}):[/bold]")
                for vm in sorted(vms, key=lambda x: x.get("vmid", 0)):
                    vmid = vm.get("vmid", "-")
                    name = vm.get("name", "")
                    node = vm.get("node", "")
                    display = f"  {vmid}"
                    if name:
                        display += f" - {name}"
                    if node:
                        display += f" (node: {node})"
                    lines.append(display)

            if cts:
                lines.append("")
                lines.append(f"[bold]Containers ({len(cts)}):[/bold]")
                for ct in sorted(cts, key=lambda x: x.get("vmid", 0)):
                    vmid = ct.get("vmid", "-")
                    name = ct.get("name", "")
                    node = ct.get("node", "")
                    display = f"  {vmid}"
                    if name:
                        display += f" - {name}"
                    if node:
                        display += f" (node: {node})"
                    lines.append(display)

            if storages:
                lines.append("")
                lines.append(f"[bold]Storages ({len(storages)}):[/bold]")
                for storage in sorted(storages, key=lambda x: x.get("storage", "")):
                    storage_id = storage.get("storage", "-")
                    lines.append(f"  {storage_id}")

            if not vms and not cts and not storages:
                lines.append("")
                lines.append("[dim]No members in this pool[/dim]")

            panel = Panel(
                "\n".join(lines),
                title=f"Pool: {poolid}",
                border_style="blue",
            )
            console.print(panel)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# ── pool add / remove (create/delete pools) ─────────────────────────────


@app.command("add")
@async_to_sync
async def add_pool(
    poolid: str = typer.Argument(None, help="Pool ID(s) - single or comma-separated (e.g., dev or dev,staging,prod)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    comment: str = typer.Option(None, "--comment", "-c", help="Pool description"),
) -> None:
    """Create one or more resource pools."""
    config_manager = ConfigManager()

    try:
        pool_ids: list[str] = []

        if poolid:
            pool_ids = [p.strip() for p in poolid.split(",") if p.strip()]
        else:
            # Interactive mode: ask for pool names in a loop
            while True:
                name = prompt("  Pool ID")
                if not name.strip():
                    print_error("Pool ID cannot be empty")
                    raise typer.Exit(1)
                pool_ids.append(name.strip())
                if not confirm("  Create another pool?", default=False):
                    break

        if not pool_ids:
            print_error("No pool IDs provided")
            raise typer.Exit(1)

        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            for pid in pool_ids:
                data: dict[str, str] = {"poolid": pid}
                if comment:
                    data["comment"] = comment

                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=console,
                ) as progress:
                    progress.add_task(description=f"Creating pool '{pid}'...", total=None)
                    await client.post("/pools", data=data)

                print_success(f"Pool '{pid}' created successfully")

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("remove")
@async_to_sync
async def remove_pool(
    poolid: str = typer.Argument(None, help="Pool ID"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    force: bool = typer.Option(False, "--force", "-f", help="Delete even if pool contains resources"),
) -> None:
    """Delete one or more resource pools."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            pools = await client.get("/pools")
            if isinstance(pools, dict) and "data" in pools:
                pools = pools.get("data", [])
            elif not isinstance(pools, list):
                pools = []

            if not pools:
                print_warning("No pools found")
                return

            pool_ids = sorted(p.get("poolid", "") for p in pools)

            if poolid is None:
                sel = multi_select_menu(pool_ids, "  Pools to remove (Space to toggle, Enter to confirm):")
                if sel is None:
                    print_cancelled()
                    return
                selected_pools = [pool_ids[i] for i in sel]
                if not selected_pools:
                    print_cancelled()
                    return
            else:
                selected_pools = [poolid]

            # Check each pool for members
            blocked = []
            empty = []
            for pid in selected_pools:
                pool_data = await client.get(f"/pools/{pid}")
                if isinstance(pool_data, dict) and "data" in pool_data:
                    pool_data = pool_data.get("data", {})
                members = pool_data.get("members", [])
                if members and not force:
                    blocked.append((pid, members))
                else:
                    empty.append(pid)

            if blocked:
                for pid, members in blocked:
                    vms = [m for m in members if m.get("type") == "qemu"]
                    cts = [m for m in members if m.get("type") == "lxc"]
                    storages = [m for m in members if m.get("type") == "storage"]
                    parts = []
                    if vms:
                        parts.append(f"{len(vms)} VM(s)")
                    if cts:
                        parts.append(f"{len(cts)} CT(s)")
                    if storages:
                        parts.append(f"{len(storages)} storage(s)")
                    print_warning(f"Pool '{pid}' contains {', '.join(parts)} — use --force to delete anyway")

            if not empty:
                return

            if not yes:
                if len(empty) == 1:
                    msg = f"Delete pool '{empty[0]}'? This cannot be undone!"
                else:
                    msg = f"Delete {len(empty)} pools ({', '.join(empty)})? This cannot be undone!"
                if not confirm(msg, default=False):
                    print_cancelled()
                    return

            deleted = 0
            for pid in empty:
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=console,
                ) as progress:
                    progress.add_task(description=f"Deleting pool '{pid}'...", total=None)
                    await client.delete(f"/pools/{pid}")
                deleted += 1

            if deleted == 1:
                print_success(f"Pool '{empty[0]}' deleted successfully")
            else:
                print_success(f"{deleted} pools deleted: {', '.join(empty)}")

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# ── pool content add / remove (manage pool members) ─────────────────────


def _format_resource(r: dict) -> str:
    """Format a resource dict as 'TYPE VMID (name)'."""
    rtype = "VM" if r.get("type") == "qemu" else "CT"
    vmid = r.get("vmid", "?")
    name = r.get("name", "")
    return f"{rtype} {vmid} ({name})" if name else f"{rtype} {vmid}"


async def _pick_pool(client: ProxmoxClient) -> str | None:
    """Fetch pools and show a single-select menu. Returns poolid or None."""
    pools = await client.get("/pools")
    if isinstance(pools, dict) and "data" in pools:
        pools = pools.get("data", [])
    elif not isinstance(pools, list):
        pools = []

    if not pools:
        print_warning("No pools found")
        return None

    pool_ids = sorted(p.get("poolid", "") for p in pools)
    idx = select_menu(pool_ids, "  Select pool:")
    if idx is None:
        print_cancelled()
        return None
    return pool_ids[idx]


@content_app.command("add")
@async_to_sync
async def content_add(
    poolid: str = typer.Argument(None, help="Pool ID"),
    vmids: str = typer.Argument(None, help="VM or Container ID(s) (comma-separated, e.g. 100,101,102)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    force: bool = typer.Option(False, "--force", "-f", help="Allow moving VM/CT from another pool"),
) -> None:
    """Add one or more VMs or Containers to a pool.

    By default, adding a VM/CT that is already in another pool will fail.
    Use --force to allow moving it from its current pool.
    """
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            # Interactive pool selection
            if not poolid:
                poolid = await _pick_pool(client)
                if poolid is None:
                    return

            # Get all resources
            resources = await client.get_cluster_resources(resource_type="vm")

            if vmids is None:
                # Filter resources not already in any pool
                available = [r for r in resources if not r.get("pool")]
                available = sorted(available, key=lambda r: r.get("vmid", 0))

                if not available:
                    print_warning(f"No VMs/CTs available to add to pool '{poolid}'")
                    return

                labels = [_format_resource(r) for r in available]
                sel = multi_select_menu(labels, f"  Add to pool '{poolid}' (Space to toggle, Enter to confirm):")
                if sel is None:
                    print_cancelled()
                    return
                if not sel:
                    print_cancelled()
                    return

                added_items = []
                for i in sel:
                    r = available[i]
                    rtype = "VM" if r.get("type") == "qemu" else "Container"
                    added_items.append({"vmid": r.get("vmid"), "type": rtype, "name": r.get("name", "")})
            else:
                # Parse input VMIDs (comma-separated)
                try:
                    vmid_list = [int(v.strip()) for v in vmids.split(",") if v.strip()]
                except ValueError:
                    print_error("Invalid VMID format. Use comma-separated numbers: 100,101,102")
                    raise typer.Exit(1)

                if not vmid_list:
                    print_error("No valid VMIDs provided")
                    raise typer.Exit(1)

                added_items = []
                not_found = []
                for vmid in vmid_list:
                    resource = next((r for r in resources if r.get("vmid") == vmid), None)
                    if resource:
                        rtype = "VM" if resource.get("type") == "qemu" else "Container"
                        added_items.append({"vmid": vmid, "type": rtype, "name": resource.get("name", "")})
                    else:
                        not_found.append(vmid)

                if not_found:
                    for nf in not_found:
                        print_warning(f"VM/Container {nf} not found")

                if not added_items:
                    print_error("No valid VMs/Containers to add")
                    raise typer.Exit(1)

            # Add to pool (API accepts comma-separated string)
            valid_vmids = [item["vmid"] for item in added_items]
            data = {"vms": ",".join(map(str, valid_vmids))}

            if force:
                data["allow-move"] = 1

            try:
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=console,
                ) as progress:
                    if len(added_items) == 1:
                        item = added_items[0]
                        desc = f"Adding {item['type']} {item['vmid']} to pool '{poolid}'..."
                    else:
                        desc = f"Adding {len(added_items)} items to pool '{poolid}'..."
                    progress.add_task(description=desc, total=None)
                    await client.put(f"/pools/{poolid}", data=data)
            except PVECliError as e:
                error_msg = str(e)
                if "allow-move" in error_msg.lower() or "is already a member" in error_msg.lower():
                    print_error("One or more VMs/CTs are already in another pool")
                    print_info("Use --force to allow moving them from their current pool")
                    raise typer.Exit(1)
                else:
                    raise

            # Success messages
            if len(added_items) == 1:
                item = added_items[0]
                success_msg = f"{item['type']} {item['vmid']}"
                if item['name']:
                    success_msg += f" ({item['name']})"
                success_msg += f" added to pool '{poolid}'"
                print_success(success_msg)
            else:
                print_success(f"{len(added_items)} items added to pool '{poolid}':")
                for item in added_items:
                    display = f"  - {item['type']} {item['vmid']}"
                    if item['name']:
                        display += f" ({item['name']})"
                    console.print(display)

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@content_app.command("remove")
@async_to_sync
async def content_remove(
    poolid: str = typer.Argument(None, help="Pool ID"),
    vmids: str = typer.Argument(None, help="VM or Container ID(s) (comma-separated, e.g. 100,101,102)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Remove one or more VMs or Containers from a pool."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            # Interactive pool selection
            if not poolid:
                poolid = await _pick_pool(client)
                if poolid is None:
                    return

            # Fetch pool members
            pool_data = await client.get(f"/pools/{poolid}")
            if isinstance(pool_data, dict) and "data" in pool_data:
                pool_data = pool_data.get("data", {})
            members = [m for m in pool_data.get("members", []) if m.get("type") in ("qemu", "lxc")]
            members = sorted(members, key=lambda m: m.get("vmid", 0))

            if not members:
                print_warning(f"Pool '{poolid}' has no VMs/CTs")
                return

            if vmids is None:
                labels = [_format_resource(m) for m in members]
                sel = multi_select_menu(labels, f"  Remove from pool '{poolid}' (Space to toggle, Enter to confirm):")
                if sel is None:
                    print_cancelled()
                    return
                if not sel:
                    print_cancelled()
                    return

                removed_items = []
                for i in sel:
                    m = members[i]
                    rtype = "VM" if m.get("type") == "qemu" else "Container"
                    removed_items.append({"vmid": m.get("vmid"), "type": rtype, "name": m.get("name", "")})
            else:
                try:
                    vmid_list = [int(v.strip()) for v in vmids.split(",") if v.strip()]
                except ValueError:
                    print_error("Invalid VMID format. Use comma-separated numbers: 100,101,102")
                    raise typer.Exit(1)

                if not vmid_list:
                    print_error("No valid VMIDs provided")
                    raise typer.Exit(1)

                member_map = {m.get("vmid"): m for m in members}
                removed_items = []
                not_found = []
                for vmid in vmid_list:
                    m = member_map.get(vmid)
                    if m:
                        rtype = "VM" if m.get("type") == "qemu" else "Container"
                        removed_items.append({"vmid": vmid, "type": rtype, "name": m.get("name", "")})
                    else:
                        not_found.append(vmid)

                if not_found:
                    for nf in not_found:
                        print_warning(f"VM/Container {nf} not found in pool '{poolid}'")

                if not removed_items:
                    print_error("No valid VMs/Containers to remove")
                    raise typer.Exit(1)

            # Confirm removal
            if not yes:
                if len(removed_items) == 1:
                    item = removed_items[0]
                    msg = f"Remove {item['type']} {item['vmid']}"
                    if item['name']:
                        msg += f" ({item['name']})"
                    msg += f" from pool '{poolid}'?"
                else:
                    msg = f"Remove {len(removed_items)} items from pool '{poolid}'?"
                if not confirm(msg):
                    print_cancelled()
                    return

            valid_vmids = [item["vmid"] for item in removed_items]
            data = {
                "vms": ",".join(map(str, valid_vmids)),
                "delete": 1
            }

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                if len(removed_items) == 1:
                    item = removed_items[0]
                    desc = f"Removing {item['type']} {item['vmid']} from pool '{poolid}'..."
                else:
                    desc = f"Removing {len(removed_items)} items from pool '{poolid}'..."
                progress.add_task(description=desc, total=None)
                await client.put(f"/pools/{poolid}", data=data)

            # Success messages
            if len(removed_items) == 1:
                item = removed_items[0]
                success_msg = f"{item['type']} {item['vmid']}"
                if item['name']:
                    success_msg += f" ({item['name']})"
                success_msg += f" removed from pool '{poolid}'"
                print_success(success_msg)
            else:
                print_success(f"{len(removed_items)} items removed from pool '{poolid}':")
                for item in removed_items:
                    display = f"  - {item['type']} {item['vmid']}"
                    if item['name']:
                        display += f" ({item['name']})"
                    console.print(display)

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
