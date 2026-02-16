"""Tag parsing and collection utilities."""


def parse_tags(tags_str: str) -> list[str]:
    """Parse a semicolon-separated tags string into a list."""
    return [t.strip() for t in tags_str.split(";") if t.strip()]


def join_tags(tags: list[str]) -> str:
    """Join a tags list into a semicolon-separated string."""
    return ";".join(tags)


def format_tags_colored(tags_str: str, color_map: dict[str, str]) -> str:
    """Format tags with Rich markup using color-map colors.

    Color map values use Proxmox format: "bg_hex" or "bg_hex:fg_hex".

    Args:
        tags_str: Semicolon-separated tags string from Proxmox.
        color_map: Dict mapping tag name to color string (e.g. "cc3333:FFFFFF").

    Returns:
        Rich-formatted string with colored tag badges, or "-" if no tags.
    """
    if not tags_str:
        return "-"
    tags = parse_tags(tags_str)
    if not tags:
        return "-"
    parts = []
    for tag in tags:
        color = color_map.get(tag, "")
        if color:
            color_parts = color.split(":")
            bg = color_parts[0]
            fg = color_parts[1] if len(color_parts) > 1 else "FFFFFF"
            parts.append(f"[on #{bg}][#{fg}] {tag} [/]")
        else:
            parts.append(f"[dim]{tag}[/dim]")
    return " ".join(parts)
