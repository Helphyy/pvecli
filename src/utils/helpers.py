"""Helper utilities."""

import asyncio
from functools import wraps
from typing import Any, Callable

from typer.core import TyperGroup


def async_to_sync(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator to run async functions synchronously."""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(func(*args, **kwargs))

    return wrapper


def ordered_group(order: list[str]) -> type[TyperGroup]:
    """Create a TyperGroup subclass that orders commands."""

    class _OrderedGroup(TyperGroup):
        def list_commands(self, ctx: Any) -> list[str]:
            commands = super().list_commands(ctx)
            rank = {n: i for i, n in enumerate(order)}
            return sorted(commands, key=lambda n: rank.get(n, 99))

    return _OrderedGroup


def open_browser_window(url: str) -> None:
    """Open URL in a new browser window (not tab).

    Args:
        url: URL to open
    """
    import subprocess
    import shutil
    import webbrowser

    # Try Firefox with --new-window flag
    firefox_path = shutil.which("firefox")
    if firefox_path:
        try:
            subprocess.Popen([firefox_path, "--new-window", url],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            return
        except Exception:
            pass

    # Try Chrome/Chromium with --new-window flag
    for browser in ["google-chrome", "chromium", "chromium-browser"]:
        browser_path = shutil.which(browser)
        if browser_path:
            try:
                subprocess.Popen([browser_path, "--new-window", url],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
                return
            except Exception:
                pass

    # Fallback to default browser
    webbrowser.open_new(url)
