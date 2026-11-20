"""Helper utilities."""

import asyncio
import signal
import threading
from collections.abc import Callable
from functools import wraps
from types import FrameType
from typing import Any

from typer.core import TyperGroup


def _waiting_on_io(frame: FrameType | None) -> bool:
    """Whether a signal arrived while the event loop waited for the network."""
    return frame is not None and frame.f_code.co_filename.endswith("selectors.py")


def async_to_sync(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator to run async functions synchronously.

    asyncio.run turns Ctrl+C into a cancellation of the task. That is right
    while a request is in flight (handlers stop the Proxmox task), but a
    menu or a prompt blocks the loop and never sees the cancellation: the
    first Ctrl+C was lost. Ctrl+C now cancels the task only while the loop
    waits on the network, and raises KeyboardInterrupt anywhere else.
    """

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if threading.current_thread() is not threading.main_thread():
            return asyncio.run(func(*args, **kwargs))

        async def main() -> Any:
            cancel_task = signal.getsignal(signal.SIGINT)

            def on_sigint(signum: int, frame: FrameType | None) -> None:
                if _waiting_on_io(frame) and callable(cancel_task):
                    cancel_task(signum, frame)
                    return
                raise KeyboardInterrupt

            signal.signal(signal.SIGINT, on_sigint)
            return await func(*args, **kwargs)

        previous = signal.getsignal(signal.SIGINT)
        try:
            return asyncio.run(main())
        finally:
            signal.signal(signal.SIGINT, previous)

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
    import shutil
    import subprocess
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
