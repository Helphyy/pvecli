"""Global tag management commands."""

from pathlib import Path

import typer
import yaml
from rich.table import Table
from ..api.client import ProxmoxClient
from ..api.exceptions import PVECliError
from ..config import ConfigManager
from ..utils import confirm, console, print_cancelled, print_error, print_success, print_warning, prompt
from ..utils.helpers import async_to_sync, ordered_group
from ..utils.menu import multi_select_menu, select_menu

app = typer.Typer(help="Manage tags globally", no_args_is_help=True, cls=ordered_group(["add", "remove", "color", "list"]))
color_app = typer.Typer(help="Manage color palette", no_args_is_help=True, cls=ordered_group(["add", "remove", "init", "list"]))
app.add_typer(color_app, name="color")

# Default color palette used by `tag color init`
_DEFAULT_PALETTE = {
    "Red": {"hex": "cc3333", "font": "white"},
    "Green": {"hex": "2d8f2d", "font": "white"},
    "Blue": {"hex": "2266cc", "font": "white"},
    "Orange": {"hex": "cc6600", "font": "white"},
    "Purple": {"hex": "7733aa", "font": "white"},
    "Teal": {"hex": "1a8a8a", "font": "white"},
    "Pink": {"hex": "b33d72", "font": "white"},
    "Brown": {"hex": "8b5c2a", "font": "white"},
    "Slate": {"hex": "556677", "font": "white"},
}

_COLOR_FILE = Path.home() / ".config" / "pvecli" / "tag_color.yml"


def _load_palette() -> dict[str, dict]:
    """Load color palette from tag_color.yml."""
    if not _COLOR_FILE.exists():
        return {}
    try:
        with open(_COLOR_FILE) as f:
            data = yaml.safe_load(f) or {}
        return data
    except Exception:
        return {}


def _save_palette(palette: dict[str, dict]) -> None:
    """Save color palette to tag_color.yml."""
    _COLOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for name in sorted(palette):
        entry = palette[name]
        lines.append(f"{name}:")
        lines.append(f'  font: "{entry.get("font", "white")}"')
        lines.append(f'  hex: "{entry.get("hex", "888888")}"')
    with open(_COLOR_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")


def _get_full_palette() -> list[tuple[str, str, str]]:
    """Get color palette as list of (name, hex, font_hex)."""
    palette = _load_palette()
    result = []
    for name, entry in palette.items():
        hex_color = entry.get("hex", "888888").lstrip("#")
        font = entry.get("font", "white")
        font_hex = "ffffff" if font == "white" else "000000"
        result.append((name, hex_color, font_hex))
    return result


def _parse_color_map(tag_style) -> dict[str, str]:
    """Parse color-map from tag-style (dict or string)."""
    colors = {}
    if not tag_style:
        return colors

    if isinstance(tag_style, dict):
        raw = tag_style.get("color-map", "")
    elif isinstance(tag_style, str) and "color-map=" in tag_style:
        for part in tag_style.split(","):
            part = part.strip()
            if part.startswith("color-map="):
                raw = part[len("color-map="):]
                break
        else:
            return colors
    else:
        raw = str(tag_style)

    if not raw:
        return colors
    for entry in raw.split(";"):
        entry = entry.strip()
        if ":" in entry:
            tag, color = entry.split(":", 1)
            colors[tag.strip()] = color.strip()
    return colors


def _build_tag_style(color_map: dict[str, str], existing_style) -> str:
    """Rebuild tag-style string with updated color-map."""
    if color_map:
        return "color-map=" + ";".join(f"{tag}:{color}" for tag, color in sorted(color_map.items()))
    return ""


def _pick_color(tag_name: str) -> str | None:
    """Interactive color picker with arrow navigation and Rich-colored entries."""
    import os
    import select
    import sys
    import termios
    import tty
    from io import StringIO

    from rich.console import Console as RichConsole

    palette = _get_full_palette()
    if not palette:
        print_warning("No colors available. Run 'pvecli tag color init' first.")
        return None

    selected = 0
    n = len(palette)
    menu_lines = n + 2  # title + blank + entries

    def _render():
        buf = StringIO()
        rc = RichConsole(file=buf, force_terminal=True, width=console.width)
        rc.print(f"  Color for '[cyan]{tag_name}[/cyan]':")
        rc.print()
        for i, (cname, c, font) in enumerate(palette):
            if i == selected:
                rc.print(f"  [cyan]>[/cyan] [on #{c}][#{font}] {cname} [/][#{font}] (#{c})[/]")
            else:
                rc.print(f"    [on #{c}][#{font}] {cname} [/][#{font}] (#{c})[/]")
        return buf.getvalue()

    fd = sys.stdin.fileno()

    def _read_key():
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = os.read(fd, 1)
            if ch == b"\x1b":
                if select.select([fd], [], [], 0.1)[0]:
                    ch += os.read(fd, 1)
                    if ch[-1:] == b"[" and select.select([fd], [], [], 0.1)[0]:
                        ch += os.read(fd, 1)
            return ch.decode()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    sys.stdout.write("\x1b[?25l")  # Hide cursor
    sys.stdout.write(_render())
    sys.stdout.flush()

    try:
        while True:
            key = _read_key()
            if key == "\x1b[A":  # Up
                selected = (selected - 1) % n
            elif key == "\x1b[B":  # Down
                selected = (selected + 1) % n
            elif key in ("\r", "\n"):  # Enter
                sys.stdout.write(f"\x1b[{menu_lines}A\x1b[J")
                sys.stdout.write("\x1b[?25h")
                sys.stdout.flush()
                return palette[selected][1]
            elif key in ("\x1b", "\x03"):  # Esc or Ctrl+C
                sys.stdout.write(f"\x1b[{menu_lines}A\x1b[J")
                sys.stdout.write("\x1b[?25h")
                sys.stdout.flush()
                print_cancelled()
                return None
            else:
                continue

            # Redraw
            sys.stdout.write(f"\x1b[{menu_lines}A\x1b[J")
            sys.stdout.write(_render())
            sys.stdout.flush()
    except (KeyboardInterrupt, EOFError):
        sys.stdout.write("\x1b[?25h\n")
        sys.stdout.flush()
        print_cancelled()
        return None


def _count_tags_from_resources(resources: list[dict]) -> dict[str, dict]:
    """Count tags from pre-fetched resources."""
    tag_counts: dict[str, dict] = {}
    for r in resources:
        tags_str = r.get("tags", "")
        if not tags_str:
            continue
        rtype = "vms" if r.get("type") == "qemu" else "cts"
        for tag in tags_str.split(";"):
            tag = tag.strip()
            if not tag:
                continue
            if tag not in tag_counts:
                tag_counts[tag] = {"vms": 0, "cts": 0}
            tag_counts[tag][rtype] += 1
    return tag_counts


async def _collect_all_tags(client: ProxmoxClient) -> dict[str, dict]:
    """Collect all tags from cluster resources.

    Returns dict of {tag: {"vms": count, "cts": count}}.
    """
    resources = await client.get_cluster_resources(resource_type="vm")
    return _count_tags_from_resources(resources)


# ── tag list / add / remove ──────────────────────────────────────────────


@app.command("list")
@async_to_sync
async def list_tags(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """List all tags in the cluster."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            tag_counts = await _collect_all_tags(client)
            options = await client.get_cluster_options()
            color_map = _parse_color_map(options.get("tag-style", ""))

            # Merge tags from color-map that may not be in use
            all_tags = set(tag_counts) | set(color_map)

            if not all_tags:
                console.print("No tags found")
                return

            table = Table(title="Cluster Tags", show_header=True, header_style="bold cyan")
            table.add_column("Tag", style="cyan")
            table.add_column("Color")
            table.add_column("VMs", justify="right")
            table.add_column("CTs", justify="right")
            table.add_column("Total", justify="right")

            for tag in sorted(all_tags):
                counts = tag_counts.get(tag, {"vms": 0, "cts": 0})
                color = color_map.get(tag, "")
                if color:
                    parts = color.split(":")
                    bg = parts[0]
                    fg = parts[1] if len(parts) > 1 else "FFFFFF"
                    color_display = f"[on #{bg}][#{fg}] #{bg} [/]"
                else:
                    color_display = "-"
                total = counts["vms"] + counts["cts"]
                table.add_row(
                    tag,
                    color_display,
                    str(counts["vms"]),
                    str(counts["cts"]),
                    str(total),
                )

            console.print(table)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("add")
@async_to_sync
async def add_tag(
    tag: str = typer.Argument(None, help="Tag name"),
    color: str = typer.Option(None, "--color", "-c", help="Hex color (e.g. ff4444)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Add or update a tag color in the cluster."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        if not tag:
            tag = prompt("  Tag name")
            if not tag.strip():
                print_error("Tag name cannot be empty")
                raise typer.Exit(1)
            tag = tag.strip()

        if not color:
            color = _pick_color(tag)
            if color is None:
                return

        # Strip leading # if provided
        color = color.lstrip("#")

        async with ProxmoxClient(profile_config) as client:
            options = await client.get_cluster_options()
            existing_style = options.get("tag-style", "")
            color_map = _parse_color_map(existing_style)
            color_map[tag] = color
            new_style = _build_tag_style(color_map, existing_style)
            await client.update_cluster_options(**{"tag-style": new_style})

        print_success(f"Tag '{tag}' color set to #{color}")

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


@app.command("remove")
@async_to_sync
async def remove_tag(
    tag: str = typer.Argument(None, help="Tag(s) - single or comma/semicolon-separated (e.g., web or web,db,cache)"),
    yes: bool = typer.Option(False, "--yes", "-y", is_flag=True, help="Skip confirmation"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Remove one or more tags from all VMs and CTs in the cluster."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)

        async with ProxmoxClient(profile_config) as client:
            resources = await client.get_cluster_resources(resource_type="vm")
            tag_counts = _count_tags_from_resources(resources)
            options = await client.get_cluster_options()
            existing_style = options.get("tag-style", "")
            color_map = _parse_color_map(existing_style)
            all_tags = sorted(set(tag_counts) | set(color_map))

            if not all_tags:
                print_warning("No tags found in the cluster")
                return

            if tag is None:
                sel = multi_select_menu(all_tags, "  Tags to remove (Space to toggle, Enter to confirm):")
                if sel is None:
                    print_cancelled()
                    return
                selected_tags = [all_tags[i] for i in sel]
                if not selected_tags:
                    print_cancelled()
                    return
            else:
                import re
                selected_tags = [t.strip() for t in re.split(r"[,;]", tag) if t.strip()]

            # Collect affected resources and color info for all selected tags
            total_affected = 0
            tag_details: list[tuple[str, list, bool]] = []
            for t in selected_tags:
                affected = []
                for r in resources:
                    tags_str = r.get("tags", "")
                    if not tags_str:
                        continue
                    tags = [x.strip() for x in tags_str.split(";") if x.strip()]
                    if t in tags:
                        affected.append(r)
                has_color = t in color_map
                tag_details.append((t, affected, has_color))
                total_affected += len(affected)

            # Show summary
            for t, affected, has_color in tag_details:
                if not affected and not has_color:
                    print_warning(f"Tag '{t}' not found anywhere")
                    continue
                if affected:
                    console.print(f"\nTag '{t}' found on {len(affected)} resource(s):")
                    for r in affected:
                        rtype = "VM" if r.get("type") == "qemu" else "CT"
                        name = r.get("name", "")
                        console.print(f"  {rtype} {r.get('vmid', '?')} ({name})")
                if has_color:
                    c = color_map[t]
                    bg = c.split(":")[0]
                    fg = c.split(":")[1] if ":" in c else "FFFFFF"
                    console.print(f"  Color: [on #{bg}][#{fg}] #{bg} [/]")

            # Filter to tags that actually exist somewhere
            actionable = [(t, affected, has_color) for t, affected, has_color in tag_details if affected or has_color]
            if not actionable:
                return

            tag_names = ", ".join(f"'{t}'" for t, _, _ in actionable)
            if not yes and not confirm(f"\nRemove tag(s) {tag_names} from all resources?"):
                print_cancelled()
                return

            # Remove tags from resources
            total_removed = 0
            color_removed = 0
            for t, affected, has_color in actionable:
                for r in affected:
                    node = r.get("node", "")
                    vmid = r.get("vmid")
                    tags_str = r.get("tags", "")
                    tags = [x.strip() for x in tags_str.split(";") if x.strip()]
                    tags.remove(t)
                    new_tags = ";".join(tags)

                    if r.get("type") == "qemu":
                        await client.update_vm_config(node, vmid, tags=new_tags)
                    else:
                        await client.update_container_config(node, vmid, tags=new_tags)
                total_removed += len(affected)

                if has_color:
                    del color_map[t]
                    color_removed += 1

            # Update color map once
            if color_removed:
                new_style = _build_tag_style(color_map, existing_style)
                if new_style:
                    await client.update_cluster_options(**{"tag-style": new_style})
                else:
                    await client.update_cluster_options(**{"tag-style": ""})

            if len(actionable) == 1:
                t = actionable[0][0]
                print_success(f"Tag '{t}' removed from {total_removed} resource(s)" + (f" + color mapping" if actionable[0][2] else ""))
            else:
                names = ", ".join(t for t, _, _ in actionable)
                suffix = f" + {color_removed} color mapping(s)" if color_removed else ""
                print_success(f"{len(actionable)} tags removed ({names}) from {total_removed} resource(s){suffix}")

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# ── tag color init / list / add / remove ─────────────────────────────────


@color_app.command("init")
def color_init(
    force: bool = typer.Option(False, "--force", "-f", is_flag=True, help="Overwrite existing file"),
) -> None:
    """Initialize the color palette file with default colors."""
    if _COLOR_FILE.exists() and not force:
        if not confirm(f"  {_COLOR_FILE} already exists. Overwrite?"):
            print_cancelled()
            return

    _save_palette(_DEFAULT_PALETTE)
    print_success(f"Color palette initialized at {_COLOR_FILE}")


@color_app.command("list")
def color_list() -> None:
    """List all colors in the palette."""
    palette = _load_palette()

    if not palette:
        print_warning(f"No colors configured. Run 'pvecli tag color init' to create {_COLOR_FILE}")
        return

    table = Table(title="Color Palette", show_header=True, header_style="bold cyan")
    table.add_column("Name")
    table.add_column("Preview")
    table.add_column("Hex")
    table.add_column("Font")

    for name, entry in palette.items():
        hex_color = entry.get("hex", "888888").lstrip("#")
        font = entry.get("font", "white")
        font_hex = "ffffff" if font == "white" else "000000"
        preview = f"[on #{hex_color}][#{font_hex}]  {name}  [/]"
        table.add_row(name, preview, f"#{hex_color}", font)

    console.print(table)


@color_app.command("add")
def color_add(
    name: str = typer.Argument(None, help="Color name (e.g. 'Cyan')"),
    hex_color: str = typer.Argument(None, help="Hex color code (e.g. 00bcd4)"),
    font: str = typer.Option(None, "--font", "-f", help="Font color: white or black"),
) -> None:
    """Add a color to the palette."""
    try:
        if not _COLOR_FILE.exists():
            print_warning(f"Color palette not found. Run 'pvecli tag color init' first.")
            raise typer.Exit(1)

        if not name:
            name = prompt("  Color name")
            if not name.strip():
                print_error("Color name cannot be empty")
                raise typer.Exit(1)
            name = name.strip()

        if not hex_color:
            while True:
                hex_color = prompt("  Hex code (e.g. 00bcd4)")
                hex_color = hex_color.strip().lstrip("#")
                if len(hex_color) == 6:
                    try:
                        int(hex_color, 16)
                        break
                    except ValueError:
                        pass
                print_error("Invalid hex color, must be 6 hex characters (e.g. 00bcd4)")
        else:
            hex_color = hex_color.lstrip("#")
            if len(hex_color) != 6:
                print_error("Hex color must be 6 characters (e.g. 00bcd4)")
                raise typer.Exit(1)
            try:
                int(hex_color, 16)
            except ValueError:
                print_error("Invalid hex color")
                raise typer.Exit(1)

        if not font:
            console.print(f"\n  Preview:")
            console.print(f"    [#ffffff on #{hex_color}] {name} [/]")
            console.print(f"    [black on #{hex_color}] {name} [/]")
            console.print()
            idx = select_menu(["white", "black"], "  Font color:")
            if idx is None:
                print_cancelled()
                return
            font = ["white", "black"][idx]

        if font not in ("white", "black"):
            print_error("Font must be 'white' or 'black'")
            raise typer.Exit(1)

        palette = _load_palette()
        palette[name] = {"hex": hex_color, "font": font}
        _save_palette(palette)

        font_hex = "ffffff" if font == "white" else "000000"
        console.print(f"  [on #{hex_color}][#{font_hex}] {name} [/][#{font_hex}] (#{hex_color}) added to palette[/]")
        print_success(f"Color '{name}' saved to {_COLOR_FILE}")

    except KeyboardInterrupt:
        console.print()
        print_cancelled()


@color_app.command("remove")
def color_remove(
    name: str = typer.Argument(None, help="Color name to remove"),
) -> None:
    """Remove a color from the palette."""
    try:
        palette = _load_palette()

        if not palette:
            print_warning(f"No colors configured. Run 'pvecli tag color init' first.")
            return

        if name is None:
            color_names = sorted(palette.keys())
            sel = multi_select_menu(color_names, "  Colors to remove (Space to toggle, Enter to confirm):")
            if sel is None:
                print_cancelled()
                return
            selected = [color_names[i] for i in sel]
            if not selected:
                console.print("[yellow]No colors selected[/yellow]")
                return
            for n in selected:
                del palette[n]
            _save_palette(palette)
            if len(selected) == 1:
                print_success(f"Color '{selected[0]}' removed from palette")
            else:
                print_success(f"{len(selected)} colors removed: {', '.join(selected)}")
            return

        if name not in palette:
            print_error(f"Color '{name}' not found in palette")
            raise typer.Exit(1)

        del palette[name]
        _save_palette(palette)
        print_success(f"Color '{name}' removed from palette")

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
