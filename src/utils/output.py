"""Output formatting utilities using Rich."""

from typing import Any

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

console = Console()


def print_error(msg: str) -> None:
    """Print an error message to the console.

    Args:
        msg: The error message to display.
    """
    console.print(f"[bold red]Error:[/bold red] {msg}")


def print_success(msg: str) -> None:
    """Print a success message to the console.

    Args:
        msg: The success message to display.
    """
    console.print(f"[bold green]\u2713[/bold green] {msg}")


def print_warning(msg: str) -> None:
    """Print a warning message to the console.

    Args:
        msg: The warning message to display.
    """
    console.print(f"[bold yellow]Warning:[/bold yellow] {msg}")


def print_info(msg: str) -> None:
    """Print an info message to the console.

    Args:
        msg: The info message to display.
    """
    console.print(f"[cyan]{msg}[/cyan]")


def print_cancelled(msg: str = "Cancelled") -> None:
    """Print a cancellation message to the console.

    Args:
        msg: The cancellation message to display.
    """
    console.print(f"[yellow]{msg}[/yellow]")


def create_table(
    title: str | None = None,
    columns: list[tuple[str, str]] | None = None,
    rows: list[list[str]] | None = None,
    show_header: bool = True,
) -> Table:
    """Create a Rich table.

    Args:
        title: Optional table title.
        columns: List of (column_name, column_style) tuples.
        rows: List of row data.
        show_header: Whether to show the header row.

    Returns:
        A configured Rich Table instance.
    """
    table = Table(title=title, show_header=show_header, header_style="bold cyan")

    if columns:
        for col_name, col_style in columns:
            table.add_column(col_name, style=col_style)

    if rows:
        for row in rows:
            table.add_row(*row)

    return table


def confirm(message: str, default: bool = False) -> bool:
    """Prompt user for confirmation.

    Args:
        message: The confirmation message to display.
        default: Default choice if user just presses enter.

    Returns:
        True if user confirmed, False otherwise.
    """
    return Confirm.ask(message, default=default)


def prompt(message: str, default: str | None = None) -> str:
    """Prompt user for text input.

    Args:
        message: The prompt message to display.
        default: Default value if user just presses enter.

    Returns:
        The user's input string.
    """
    if default is None:
        return Prompt.ask(message)
    return Prompt.ask(message, default=default)


def format_bytes(bytes_value: int) -> str:
    """Format bytes to human-readable string.

    Args:
        bytes_value: The number of bytes.

    Returns:
        Formatted string (e.g., '1.5 GB').
    """
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_value < 1024.0:
            return f"{bytes_value:.1f} {unit}"
        bytes_value /= 1024.0
    return f"{bytes_value:.1f} PB"


def format_uptime(seconds: int) -> str:
    """Format uptime in seconds to human-readable string.

    Args:
        seconds: Uptime in seconds.

    Returns:
        Formatted string (e.g., '15d 3h 22m').
    """
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or not parts:
        parts.append(f"{minutes}m")

    return " ".join(parts)


def format_percentage(value: float, decimals: int = 1) -> str:
    """Format a percentage value.

    Args:
        value: The value between 0 and 100.
        decimals: Number of decimal places.

    Returns:
        Formatted percentage string (e.g., '75.5%').
    """
    return f"{value:.{decimals}f}%"


def usage_bar(percent: float, width: int = 10, label: str = "") -> str:
    """Format a usage bar with color coding.

    Args:
        percent: Usage percentage (0-100)
        width: Bar width in characters
        label: Extra label after the bar
    """
    percent = max(0.0, min(100.0, percent))
    filled = round(percent / 100 * width)
    empty = width - filled
    color = "green" if percent < 60 else "yellow" if percent < 85 else "red"
    bar = f"[{color}]{'━' * filled}[/{color}][dim]{'━' * empty}[/dim]"
    pct = f"{percent:.0f}%"
    if label:
        return f"{bar} {pct} {label}"
    return f"{bar} {pct}"


def get_status_color(status: str) -> str:
    """Get the Rich color name for a status string.

    Args:
        status: The status string (e.g., 'running', 'stopped').

    Returns:
        Rich color name ('green', 'red', 'yellow', or 'white').
    """
    status_lower = status.lower()
    if status_lower in ["running", "active", "online"]:
        return "green"
    elif status_lower in ["stopped", "inactive", "offline"]:
        return "red"
    elif status_lower in ["paused", "suspended"]:
        return "yellow"
    else:
        return "white"
