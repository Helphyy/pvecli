"""Ctrl+C: one press is enough, and every command exits 130."""

from __future__ import annotations

import asyncio
import os
import signal
import threading
import time

import pytest
import typer

from src.utils import menu as menu_module
from src.utils.helpers import async_to_sync
from src.utils.output import exit_interrupted


@pytest.fixture(autouse=True)
def default_sigint():
    # As in a real run: asyncio only takes over SIGINT from the default
    # handler, and an earlier test may have left another one
    before = signal.signal(signal.SIGINT, signal.default_int_handler)
    yield
    signal.signal(signal.SIGINT, before)


def _sigint_in(seconds: float) -> None:
    threading.Timer(seconds, lambda: os.kill(os.getpid(), signal.SIGINT)).start()


def test_ctrl_c_in_blocking_code_raises_at_once():
    # A menu or a prompt blocks the event loop: the interrupt must reach it
    seen: list[str] = []

    @async_to_sync
    async def blocked() -> None:
        try:
            time.sleep(3)
            seen.append("not interrupted")
        except KeyboardInterrupt:
            seen.append("interrupted")
            raise

    _sigint_in(0.2)
    with pytest.raises(KeyboardInterrupt):
        blocked()
    assert seen == ["interrupted"]


def test_ctrl_c_while_waiting_on_the_network_cancels_the_task():
    # The handlers that stop a Proxmox task rely on the cancellation
    seen: list[str] = []

    @async_to_sync
    async def waiting() -> None:
        try:
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            seen.append("cancelled")
            raise

    _sigint_in(0.2)
    with pytest.raises(KeyboardInterrupt):
        waiting()
    assert seen == ["cancelled"]


def test_the_sigint_handler_is_restored():
    before = signal.getsignal(signal.SIGINT)

    @async_to_sync
    async def nothing() -> int:
        return 1

    assert nothing() == 1
    assert signal.getsignal(signal.SIGINT) is before


def test_exit_interrupted_exits_130(capsys):
    with pytest.raises(typer.Exit) as e:
        exit_interrupted()
    assert e.value.exit_code == 130


def test_ctrl_c_in_a_menu_exits_130(monkeypatch):
    class Menu:
        def show(self):
            raise KeyboardInterrupt

    with pytest.raises(typer.Exit) as e:
        menu_module._show(Menu())
    assert e.value.exit_code == 130
