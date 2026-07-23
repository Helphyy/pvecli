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


def open_browser_window(url: str, new_window: bool = True) -> None:
    """Open URL in the browser.

    Args:
        url: URL to open
        new_window: Open a dedicated browser window. When False, the URL
            opens as a tab in the already-running browser instance.
    """
    import subprocess
    import shutil
    import webbrowser

    flag = ["--new-window"] if new_window else []

    # Try Firefox
    firefox_path = shutil.which("firefox")
    if firefox_path:
        try:
            subprocess.Popen([firefox_path, *flag, url],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            return
        except (FileNotFoundError, PermissionError, OSError):
            pass

    # Try Chrome/Chromium
    for browser in ["google-chrome", "chromium", "chromium-browser"]:
        browser_path = shutil.which(browser)
        if browser_path:
            try:
                subprocess.Popen([browser_path, *flag, url],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
                return
            except (FileNotFoundError, PermissionError, OSError):
                pass

    # Fallback to default browser
    if new_window:
        webbrowser.open_new(url)
    else:
        webbrowser.open_new_tab(url)
