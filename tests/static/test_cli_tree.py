"""Every command of the CLI tree loads and answers --help.

Catches a typer or click API change that breaks startup (typer 0.29
removed `click_type`: every fresh install crashed before this test).
"""

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from src.cli.main import app


def _paths() -> list[list[str]]:
    paths: list[list[str]] = []

    def walk(cmd, path):
        paths.append(path)
        for name, sub in (getattr(cmd, "commands", None) or {}).items():
            walk(sub, path + [name])

    walk(get_command(app), [])
    return paths


PATHS = _paths()


def test_the_tree_is_not_empty():
    assert len(PATHS) > 150


@pytest.mark.parametrize("path", PATHS, ids=lambda p: " ".join(p) or "pvecli")
def test_help(path):
    result = CliRunner().invoke(app, path + ["--help"])
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output
