"""Storage management commands."""

import typer
from rich.panel import Panel
from rich.table import Table

from ..api.client import ProxmoxClient
from ..api.exceptions import PVECliError
from ..config import ConfigManager
from ..utils import confirm, console, format_bytes, format_percentage, print_cancelled, print_error, print_info, print_success, print_warning, prompt
from ..utils.helpers import async_to_sync, ordered_group
from ..utils.menu import multi_select_menu, select_menu
from ._shared import pick_node

app = typer.Typer(help="Manage storage", no_args_is_help=True, cls=ordered_group(["config", "content", "list", "show"]))
content_app = typer.Typer(help="Manage storage content", no_args_is_help=True)
app.add_typer(content_app, name="content")


async def _pick_storage(client: ProxmoxClient, node: str) -> str | None:
    """Interactive single-select for a storage on a node. Returns storage id or None."""
    storages = await client.get_storage_list(node)
    storage_ids = sorted(s.get("storage", "") for s in storages if s.get("storage"))
    if not storage_ids:
        print_info(f"No storages found on node '{node}'")
        return None
    idx = select_menu(storage_ids, f"  Select storage on '{node}':")
    if idx is None:
        print_cancelled()
        return None
    return storage_ids[idx]


async def _resolve_node_storage(client: ProxmoxClient, node: str | None, storage: str | None) -> tuple[str, str] | None:
    """Resolve node and storage interactively if not provided. Returns (node, storage) or None on cancel."""
    if not node:
        node = await pick_node(client)
        if node is None:
            return None
    if not storage:
        storage = await _pick_storage(client, node)
        if storage is None:
            return None
    return node, storage


# ── storage list ─────────────────────────────────────────────────────────


@app.command("list")
@async_to_sync
async def list_storage(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    node: str = typer.Option(None, "--node", "-n", help="Show storage for specific node"),
) -> None:
    """List all storage."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if node:
                storage_list = await client.get_storage_list(node)
                title = f"Storage on {node}"
            else:
                nodes = await client.get_nodes()
                storage_list = []
                for n in nodes:
                    node_storage = await client.get_storage_list(n.get("node"))
                    for s in node_storage:
                        s["_node"] = n.get("node")
                    storage_list.extend(node_storage)
                title = "Cluster Storage"

            if not storage_list:
                print_info("No storage found")
                return

            table = Table(title=title, show_header=True, header_style="bold cyan")
            if not node:
                table.add_column("Node", style="cyan")
            table.add_column("Storage", style="cyan")
            table.add_column("Type")
            table.add_column("Content")
            table.add_column("Status")
            table.add_column("Total", justify="right")
            table.add_column("Used", justify="right")
            table.add_column("Available", justify="right")
            table.add_column("Usage %", justify="right")

            for storage in storage_list:
                row = []
                if not node:
                    row.append(storage.get("_node", "-"))

                active = storage.get("active", False)
                enabled = storage.get("enabled", True)
                if active and enabled:
                    status = "[green]active[/green]"
                elif enabled:
                    status = "[yellow]inactive[/yellow]"
                else:
                    status = "[red]disabled[/red]"

                row.extend([
                    storage.get("storage", "-"),
                    storage.get("type", "-"),
                    storage.get("content", "-"),
                    status,
                ])

                total = storage.get("total", 0)
                used = storage.get("used", 0)
                avail = storage.get("avail", 0)

                if total:
                    used_pct = (used / total * 100) if total else 0
                    row.extend([
                        format_bytes(total),
                        format_bytes(used),
                        format_bytes(avail),
                        format_percentage(used_pct),
                    ])
                else:
                    row.extend(["-", "-", "-", "-"])

                table.add_row(*row)

            console.print(table)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# ── storage show ─────────────────────────────────────────────────────────


def _render_storage_panel(node: str, storage: str, status: dict, config: dict) -> Panel:
    """Build a Rich Panel for a single storage."""
    lines = []
    lines.append("[bold]── General ──[/bold]")
    lines.append(f"[bold]Storage:[/bold]    {storage}")
    lines.append(f"[bold]Node:[/bold]       {node}")
    lines.append(f"[bold]Type:[/bold]       {status.get('type', 'unknown')}")

    active = status.get("active", False)
    enabled = status.get("enabled", True)
    if active and enabled:
        status_str = "[green]active[/green]"
    elif enabled:
        status_str = "[yellow]inactive[/yellow]"
    else:
        status_str = "[red]disabled[/red]"
    lines.append(f"[bold]Status:[/bold]     {status_str}")

    lines.append(f"[bold]Content:[/bold]    {status.get('content', '-')}")
    lines.append(f"[bold]Shared:[/bold]     {'Yes' if status.get('shared') else 'No'}")

    if 'path' in config:
        lines.append(f"[bold]Path:[/bold]       {config.get('path')}")

    total = status.get("total", 0)
    used = status.get("used", 0)
    avail = status.get("avail", 0)

    if total:
        lines.append("")
        lines.append("[bold]── Capacity ──[/bold]")
        used_pct = (used / total * 100) if total else 0
        lines.append(f"[bold]Total:[/bold]      {format_bytes(total)}")
        lines.append(f"[bold]Used:[/bold]       {format_bytes(used)} ({format_percentage(used_pct)})")
        lines.append(f"[bold]Available:[/bold]  {format_bytes(avail)}")

    return Panel("\n".join(lines), title=f"Storage: {storage}", border_style="blue")


@app.command("show")
@async_to_sync
async def show_storage(
    node: str = typer.Argument(None, help="Node name"),
    storage: str = typer.Argument(None, help="Storage ID"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Show detailed storage information and configuration."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            if not node:
                node = await pick_node(client)
                if node is None:
                    return

            if not storage:
                storages = await client.get_storage_list(node)
                storage_ids = sorted(s.get("storage", "") for s in storages if s.get("storage"))
                if not storage_ids:
                    print_info(f"No storages found on node '{node}'")
                    return
                sel = multi_select_menu(storage_ids, f"  Select storage(s) on '{node}':")
                if sel is None:
                    print_cancelled()
                    return
                if not sel:
                    print_cancelled()
                    return
                selected_storages = [storage_ids[i] for i in sel]
            else:
                selected_storages = [storage]

            for sid in selected_storages:
                status = await client.get_storage_status(node, sid)
                config = await client.get_storage_config(sid)
                console.print(_render_storage_panel(node, sid, status, config))

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# ── storage config ───────────────────────────────────────────────────────


_CONTENT_TYPES = [
    "images", "rootdir", "vztmpl", "backup", "iso", "snippets", "import",
]


@app.command("config")
@async_to_sync
async def config_storage(
    node: str = typer.Argument(None, help="Node name"),
    storage: str = typer.Argument(None, help="Storage ID"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Configure storage content types interactively."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            result = await _resolve_node_storage(client, node, storage)
            if result is None:
                return
            node, storage = result

            config = await client.get_storage_config(storage)
            storage_type = config.get('type', 'unknown')

            console.print(f"\n[bold cyan]Node:[/bold cyan]    {node}")
            console.print(f"[bold cyan]Storage:[/bold cyan] {storage}")
            console.print(f"[bold cyan]Type:[/bold cyan]    {storage_type}\n")

            current_content = config.get('content', '')
            current_types = [ct.strip() for ct in current_content.split(',') if ct.strip()]

            preselected = [i for i, name in enumerate(_CONTENT_TYPES) if name in current_types]

            sel = multi_select_menu(_CONTENT_TYPES, "  Content types (Space to toggle, Enter to confirm):", preselected=preselected)
            if sel is None:
                print_cancelled()
                return

            selected = [_CONTENT_TYPES[i] for i in sel]
            new_content = ",".join(selected)

            console.print("\n[bold cyan]Changes:[/bold cyan]")
            console.print(f"  [bold]Before:[/bold] [yellow]{current_content}[/yellow]")
            console.print(f"  [bold]After:[/bold]  [green]{new_content}[/green]")

            if not new_content:
                print_warning("No content types selected!")

            if not confirm("Apply these changes?"):
                print_cancelled()
                return

            await client.update_storage_config(storage, content=new_content)
            print_success(f"Storage '{storage}' updated successfully")

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    except KeyboardInterrupt:
        console.print()
        print_cancelled()
        raise typer.Exit(0)


# ── storage content list / add / delete ──────────────────────────────────


@content_app.command("list")
@async_to_sync
async def list_content(
    node: str = typer.Argument(None, help="Node name"),
    storage: str = typer.Argument(None, help="Storage ID"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    content_type: str = typer.Option(None, "--type", "-t", help="Filter by content type"),
) -> None:
    """List storage content."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            result = await _resolve_node_storage(client, node, storage)
            if result is None:
                return
            node, storage = result

            content = await client.get_storage_content(node, storage, content_type)

            if not content:
                print_info(f"No content found in storage '{storage}'")
                return

            table = Table(
                title=f"Content in {storage} on {node}",
                show_header=True,
                header_style="bold cyan",
            )
            table.add_column("Volume ID", style="cyan")
            table.add_column("Content")
            table.add_column("Format")
            table.add_column("Size", justify="right")
            table.add_column("VMID", justify="right")

            for item in content:
                table.add_row(
                    item.get("volid", "-"),
                    item.get("content", "-"),
                    item.get("format", "-"),
                    format_bytes(item.get("size", 0)) if item.get("size") else "-",
                    str(item["vmid"]) if item.get("vmid") else "-",
                )

            console.print(table)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@content_app.command("add")
@async_to_sync
async def add_content(
    node: str = typer.Argument(None, help="Node name"),
    storage: str = typer.Argument(None, help="Storage ID"),
    source_file: str = typer.Option(None, "--source-file", "-s", help="Path to file to upload"),
    content_type: str = typer.Option(None, "--type", "-t", help="Content type: iso, vztmpl, or import"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Upload content to storage (ISO images, container templates, or import content)."""
    config_manager = ConfigManager()

    try:
        from pathlib import Path

        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            result = await _resolve_node_storage(client, node, storage)
            if result is None:
                return
            node, storage = result

            if not source_file:
                source_file = prompt("  File path")
                if not source_file.strip():
                    print_error("File path cannot be empty")
                    raise typer.Exit(1)
                source_file = source_file.strip()

            file = Path(source_file)
            if not file.exists():
                print_error(f"File not found: {source_file}")
                raise typer.Exit(1)

            valid_types = ["iso", "vztmpl", "import"]
            if not content_type:
                idx = select_menu(valid_types, "  Content type:")
                if idx is None:
                    print_cancelled()
                    return
                content_type = valid_types[idx]
            elif content_type not in valid_types:
                print_error(f"Invalid content type '{content_type}'. Valid: {', '.join(valid_types)}")
                raise typer.Exit(1)

            file_size = file.stat().st_size

            console.print(f"\n[bold]Upload details:[/bold]")
            console.print(f"  File:    {file.name}")
            console.print(f"  Size:    {format_bytes(file_size)}")
            console.print(f"  Storage: {storage}")
            console.print(f"  Node:    {node}")
            console.print(f"  Type:    {content_type}")

            if not yes and not confirm("Proceed with upload?"):
                print_cancelled()
                return

            console.print("\n[cyan]Uploading...[/cyan]")

            try:
                upid = await client.upload_storage_content(
                    node=node,
                    storage=storage,
                    content_type=content_type,
                    file_path=str(file),
                )

                print_success("Upload started successfully")
                console.print(f"[cyan]Task ID:[/cyan] {upid}")

                console.print("[cyan]Waiting for upload to complete...[/cyan]")
                task_result = await client.wait_for_task(node, upid)

                exitstatus = task_result.get("exitstatus", "")
                if exitstatus == "OK":
                    print_success(f"File '{file.name}' uploaded to '{storage}'")
                else:
                    print_error(f"Upload failed with status: {exitstatus}")
                    raise typer.Exit(1)

            except FileNotFoundError as e:
                print_error(str(e))
                raise typer.Exit(1)

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@content_app.command("remove")
@async_to_sync
async def remove_content(
    node: str = typer.Argument(None, help="Node name"),
    storage: str = typer.Argument(None, help="Storage ID"),
    volume: str = typer.Argument(None, help="Volume ID to delete"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete content from storage."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            result = await _resolve_node_storage(client, node, storage)
            if result is None:
                return
            node, storage = result

            if not volume:
                content = await client.get_storage_content(node, storage)
                if not content:
                    print_info(f"No content in storage '{storage}'")
                    return

                labels = []
                for item in content:
                    volid = item.get("volid", "?")
                    size = format_bytes(item.get("size", 0)) if item.get("size") else "?"
                    ctype = item.get("content", "?")
                    labels.append(f"{volid} ({ctype}, {size})")

                sel = multi_select_menu(labels, f"  Volumes to delete on '{storage}':")
                if sel is None:
                    print_cancelled()
                    return
                if not sel:
                    print_cancelled()
                    return
                selected_volumes = [content[i].get("volid") for i in sel]
            else:
                selected_volumes = [volume]

            console.print(f"\n[bold red]Delete volume(s):[/bold red]")
            for vol in selected_volumes:
                console.print(f"  {vol}")
            console.print(f"  Storage: {storage}")
            console.print(f"  Node:    {node}")

            if not yes and not confirm("Delete these volumes?"):
                print_cancelled()
                return

            for vol in selected_volumes:
                await client.delete(f"/nodes/{node}/storage/{storage}/content/{vol}")

            if len(selected_volumes) == 1:
                print_success(f"Volume '{selected_volumes[0]}' deleted")
            else:
                print_success(f"{len(selected_volumes)} volumes deleted")

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
