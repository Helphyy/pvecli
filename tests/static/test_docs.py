"""Commands and options quoted in the docs exist in the CLI.

A doc reviewed by eye let through four wrong commands in August (options
that never existed, wrong argument syntax); the docs are now checked
against the real command tree.
"""

import re
import shlex
from pathlib import Path

import pytest
from typer.main import get_command

from src.cli.main import app

ROOT = Path(__file__).resolve().parents[2]
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
TOP = "vm|ct|node|tag|pool|sdn|ceph|cluster|storage|config|find|audit|snapshot"
LINE = re.compile(rf"(?:^|`|\s)(pvecli\s(?:{TOP})\b[^`\n]*)")


def _check(line: str) -> str | None:
    line = re.split(r"\s+#|\s*\||\s*>|\s*;|\s*&&|\s*<", line)[0]
    try:
        tokens = shlex.split(line)
    except ValueError:
        return None
    tokens = [t.lstrip("(").rstrip(",)") for t in tokens[1:]]
    cmd, path = get_command(app), []
    while tokens and getattr(cmd, "commands", None) and tokens[0] in cmd.commands:
        path.append(tokens[0])
        cmd = cmd.commands[tokens.pop(0)]
    if getattr(cmd, "commands", None) and tokens and not tokens[0].startswith("-"):
        return f"unknown subcommand '{tokens[0]}' under '{' '.join(path)}'"
    opts = {o for p in cmd.params for o in (p.opts + p.secondary_opts)} | {"--help"}
    for t in tokens:
        if t == "--":
            break
        if t.startswith("-") and not re.fullmatch(r"-\d.*", t):
            for part in t.split("=")[0].split("/"):
                if part and part not in opts:
                    return f"unknown option '{part}' for '{' '.join(path)}'"
    return None


def _lines():
    for doc in DOCS:
        for n, text in enumerate(doc.read_text().splitlines(), 1):
            for m in LINE.finditer(text):
                yield pytest.param(m.group(1).strip(), id=f"{doc.name}:{n}")


@pytest.mark.parametrize("line", list(_lines()))
def test_documented_command_exists(line):
    assert _check(line) is None, f"{_check(line)}: {line}"
