"""Shared fixtures: a fake Proxmox API, a fake clock and an isolated CLI.

Every test runs with HOME pointing at a temporary directory holding its own
config.yaml, so the user's real profiles (and production clusters) are never
read nor written.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from tests.fake_api import FakeAPI, install

# Profiles of the test config. h1 and h2 are two clusters; p3 is a second
# profile on the h1 cluster; prod is kept out of patterns and groups.
TEST_CONFIG: dict[str, Any] = {
    "default_profile": "p1",
    "groups": {"labs": ["p1", "p2"], "withprod": ["p1", "prod"]},
    "profiles": {
        name: {
            "host": host,
            "verify_ssl": False,
            "auth": {"type": "password", "user": "root@pam", "password": "secret"},
            **extra,
        }
        for name, host, extra in (
            ("p1", "h1", {"sdn_write": True}),
            ("p2", "h2", {}),
            ("p3", "h1", {}),
            ("prod", "hp", {"fleet": False}),
        )
    },
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated HOME with the test config.yaml."""
    cfg_dir = tmp_path / ".config" / "pvecli"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yaml").write_text(yaml.safe_dump(TEST_CONFIG))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("COLUMNS", "200")
    return tmp_path


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeAPI:
    fake = FakeAPI()
    install(monkeypatch, fake)
    return fake


class FakeClock:
    """time.time() that only moves when code sleeps."""

    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Sleeps return at once and advance the clock instead: polling loops
    and their timeouts run in microseconds."""
    c = FakeClock()
    real_sleep = asyncio.sleep

    async def fast_sleep(delay: float, result: Any = None) -> Any:
        c.now += delay
        return await real_sleep(0, result)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(time, "time", c.time)
    monkeypatch.setattr(time, "sleep", lambda d: setattr(c, "now", c.now + d))
    return c


@dataclass
class Run:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


@pytest.fixture
def cli(home: Path, api: FakeAPI, clock: FakeClock) -> Iterator[Any]:
    """cli('vm', 'list', '-p', 'p1') -> Run, against the fake API."""
    from src.cli.main import app

    runner = CliRunner()

    def run(*args: str, input: str | None = None) -> Run:
        result = runner.invoke(app, list(args), input=input)
        if result.exception and not isinstance(result.exception, SystemExit):
            raise result.exception
        return Run(result.exit_code, result.stdout, result.stderr)

    yield run
