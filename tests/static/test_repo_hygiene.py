"""House rules of the repository, enforced."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Built from code points so this file does not contain them itself
DASHES = "[" + chr(0x2013) + chr(0x2014) + "]"
TEXT = list((ROOT / "src").rglob("*.py")) + list((ROOT / "tests").rglob("*.py")) \
    + [ROOT / "README.md", *(ROOT / "docs").glob("*.md")]


def test_no_long_dashes():
    # The project writes neither em dashes nor en dashes, anywhere
    bad = [f"{p.relative_to(ROOT)}:{n}" for p in TEXT
           for n, line in enumerate(p.read_text().splitlines(), 1) if re.search(DASHES, line)]
    assert not bad, bad


def test_readme_badge_matches_the_version():
    # A regex rather than tomllib: the project still supports Python 3.10
    version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    assert f"version-{version}-" in (ROOT / "README.md").read_text()


def test_no_click_import_in_the_cli():
    # typer vendors click since 0.26: importing click directly drifts from it
    offenders = [p.name for p in (ROOT / "src").rglob("*.py") if re.search(r"^\s*(import click|from click)", p.read_text(), re.M)]
    assert not offenders
