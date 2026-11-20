"""The documentation site: generated reference up to date, captures anonymized."""

import re
from pathlib import Path

from scripts.docs.gen_reference import OUT, SECTIONS, render, render_nav

ROOT = Path(__file__).resolve().parents[2]


def test_command_reference_matches_the_cli():
    pages = render()
    stale = [name for name, text in pages.items()
             if not (OUT / name).exists() or (OUT / name).read_text() != text]
    assert not stale, f"run: python -m scripts.docs.gen_reference ({', '.join(stale)})"
    assert sorted(p.relative_to(OUT).as_posix() for p in OUT.rglob("*.md")) == sorted(pages)


def test_navigation_is_up_to_date():
    pages = render()
    nav = render_nav(pages)
    assert nav in (ROOT / "zensical.toml").read_text(), "run: python -m scripts.docs.gen_reference"
    missing = [name for name in pages if f'"cli/{name}"' not in nav]
    assert not missing


# Shapes that betray a real lab, whatever its names
SHAPES = [r"\b(?:10\.199|192\.168)\.\d+\.\d+", r"(?<![\w-])lab\d"]


def _forbidden() -> list[str]:
    """The shapes of a real lab, plus the names of the local lab if any."""
    local = ROOT / "scripts" / "docs" / "screens.local.toml"
    if not local.exists():  # CI, a contributor's clone
        return SHAPES
    import tomllib  # Python 3.11+, like the capture scripts

    extra = tomllib.loads(local.read_text()).get("forbid", [])
    return [*SHAPES, *extra]


def test_captures_hold_no_lab_name():
    # The real names stay in the git-ignored screens.local.toml
    patterns = _forbidden()
    captures = [*(ROOT / "docs" / "snippets" / "screens").glob("*.html"),
                *(ROOT / "docs" / "assets" / "casts").glob("*.cast")]
    assert captures
    for page in captures:
        text = re.sub(r"<[^>]+>", "", page.read_text())
        leaks = [p for p in patterns if re.search(p, text, flags=re.I)]
        assert not leaks, f"{page.name}: {leaks}"


def _written_pages() -> list[Path]:
    docs = ROOT / "docs"
    return [p for p in docs.rglob("*.md") if p.relative_to(docs).parts[0] != "cli"]


def test_every_written_page_is_in_the_menu():
    # A page missing from SECTIONS is built but reachable by no menu entry
    listed = {page for _, pages, _ in SECTIONS for _, page in pages} | {"index.md", "sample.md"}
    orphans = [p.relative_to(ROOT / "docs").as_posix() for p in _written_pages()]
    assert not [p for p in orphans if p not in listed], "add the page to SECTIONS"


def test_included_captures_exist():
    missing = []
    for page in _written_pages():
        text = page.read_text()
        for name in re.findall(r'--8<-- "(screens/[^"]+)"', text):
            if not (ROOT / "docs" / "snippets" / name).exists():
                missing.append(f"{page.name}: {name}")
        for name in re.findall(r'data-src="(?:\.\./)+(assets/casts/[^"]+)"', text):
            if not (ROOT / "docs" / name).exists():
                missing.append(f"{page.name}: {name}")
    assert not missing


def test_no_long_dashes():
    files = [ROOT / "README.md", ROOT / "scripts" / "docs" / "AUTHORING.md", *_written_pages()]
    found = [f.name for f in files if re.search("[\u2013\u2014]", f.read_text())]
    assert not found


def test_prerendered_diagrams_replace_their_source_in_order():
    from scripts.docs.prerender_diagrams import inline

    html = (
        '<link rel="preload" href="https://cdn.jsdelivr.net/npm/mermaid@12.3.0/dist/mermaid.min.js" as="script">\n'
        '<pre class="mermaid"><code>flowchart TD\n  A --> B</code></pre>\n'
        '<p>text</p>\n'
        '<pre class="mermaid"><code>stateDiagram-v2</code></pre>\n'
        '<script src="https://cdn.jsdelivr.net/npm/mermaid@12.3.0/dist/mermaid.min.js"></script>\n'
    )
    out = inline(html, ["<svg>1</svg>", "<svg>2</svg>"])
    assert out.index("<svg>1</svg>") < out.index("<p>text</p>") < out.index("<svg>2</svg>")
    assert '<template shadowrootmode="open"><svg>1</svg></template>' in out
    assert "mermaid@" not in out and "<pre" not in out


def test_prerender_refuses_a_count_mismatch():
    import pytest

    from scripts.docs.prerender_diagrams import inline

    with pytest.raises(ValueError):
        inline('<pre class="mermaid"><code>x</code></pre>', [])
