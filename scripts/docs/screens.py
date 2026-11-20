"""Terminal captures of the documentation, taken from real pvecli runs.

Each shot runs pvecli against a real cluster with every API answer
anonymized (scripts/docs/anon_run.py), and saves the output as an HTML
fragment that pages include with `--8<-- "screens/NAME.html"`: exact
colors, real text (selectable, found by the site search) in the site's
monospace font. Marks are texts the capture must contain, else it fails.

    python -m scripts.docs.screens                  # every shot
    python -m scripts.docs.screens cluster-health   # some shots

The lab names and the replacements live in scripts/docs/screens.local.toml
(git-ignored, see screens.example.toml): the published captures never show
them, and a shot still holding one of its `forbid` patterns is refused.
"""

from __future__ import annotations

import html
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import tomllib
from rich.console import Console
from rich.style import Style
from rich.terminal_theme import TerminalTheme
from rich.text import Text

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "snippets" / "screens"
LOCAL = Path(__file__).with_name("screens.local.toml")
WIDTH = 80  # columns: the classic terminal, readable at 13px on a page

# Close to the site's dark palette, readable in both site themes
THEME = TerminalTheme(
    (16, 22, 34), (226, 232, 240),
    [(30, 41, 59), (248, 113, 113), (74, 222, 128), (251, 191, 36),
     (96, 165, 250), (192, 132, 252), (45, 212, 191), (203, 213, 225)],
    [(100, 116, 139), (252, 165, 165), (134, 239, 172), (253, 224, 71),
     (147, 197, 253), (216, 180, 254), (94, 234, 212), (248, 250, 252)],
)


@dataclass
class Shot:
    name: str
    args: list[str]                       # pvecli arguments, {profile} replaced
    caption: str                          # what the capture shows (for the authors)
    stdin: str = ""                       # answers to the prompts
    echo: list[tuple[str, str]] = field(default_factory=list)  # (prompt end, answer shown)
    marks: list[str] = field(default_factory=list)  # texts the output must contain
    width: int = WIDTH                    # 100 for wide tables, shown in a smaller font
    cwd: str = ""                         # folder to run in, relative to the repository
    manual: bool = False                  # changes the lab or needs a setup: only run by name


def _load_shots() -> list[Shot]:
    """Every shot, from scripts/docs/shots/*.py (one file per doc section)."""
    import importlib
    import pkgutil

    from scripts.docs import shots as package

    found: list[Shot] = []
    for info in pkgutil.iter_modules(package.__path__):
        found += getattr(importlib.import_module(f"scripts.docs.shots.{info.name}"), "SHOTS", [])
    names = [s.name for s in found]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise SystemExit(f"Shot names used twice: {', '.join(sorted(dupes))}")
    return found

# A background no output uses: it tags the text of mark N in the export
_MARK_BG = "#fe00{:02x}"


def _ansi_tolerant(prompt: str) -> str:
    """A regex matching prompt even with color codes between its characters."""
    gap = r"(?:\x1b\[[0-9;]*m)*"
    return gap.join(re.escape(c) for c in prompt) + gap


# A spinner or a progress bar (rich `console.status`, `Progress`) is drawn
# frame by frame between a hide-cursor and a show-cursor code. The frames
# are not output, but a line printed while it runs (a success line) is.
_LIVE = re.compile(r"\x1b\[\?25l(.*?)\x1b\[\?25h(?:[\r\n]\x1b\[1A\x1b\[2K)?", re.S)
_CONTROL = re.compile(r"(\x1b\[2K|\x1b\[1A|\r|\n)")


def _printed_under_live(region: str) -> str:
    """The lines printed above a live display, without its frames.

    Each frame first moves up over the previous one, erasing it line by
    line, then prints the lines logged since, then the display again. So
    after the last erase only the final frame is left: what stands above
    it is what was printed. The output is read as text, so a carriage
    return comes as a newline: before an erase, it is a carriage return.
    """
    lines, row, top = [""], 0, 0
    for part in _CONTROL.split(region.replace("\n\x1b[2K", "\r\x1b[2K")):
        if part == "\x1b[2K":
            lines[row], top = "", row
        elif part == "\x1b[1A":
            row = max(row - 1, 0)
        elif part == "\n":
            row += 1
            if row == len(lines):
                lines.append("")
        elif part and part != "\r":
            lines[row] += part
    return "".join(f"{line}\n" for line in lines[:top])


def run_pvecli(args: list[str], stdin: str, width: int, cwd: str = "") -> str:
    """pvecli's colored output, every API answer anonymized.

    stdout and stderr share one pipe, so errors land where pvecli printed
    them. The doc profile names are real profiles in the anonymized run
    (anon_run.py), so pvecli lays out its headers with them.
    """
    env = {**os.environ, "FORCE_COLOR": "1", "COLUMNS": str(width), "TERM": "xterm-256color",
           "PVECLI_DOCS_ANON": str(LOCAL), "PYTHONUNBUFFERED": "1",
           "PYTHONPATH": str(ROOT)}
    run = subprocess.run(
        [sys.executable, "-m", "scripts.docs.anon_run", *args],
        input=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env=env, timeout=900, cwd=ROOT / cwd,
    )
    return _LIVE.sub(lambda m: _printed_under_live(m.group(1)), run.stdout)


def doc_names(conf: dict) -> dict[str, str]:
    """Lab profile name -> the name the documentation shows."""
    return {**conf.get("profiles", {}), conf["profile"]: conf.get("profile_as", conf["profile"])}


def _rename_profiles(text: str, names: dict[str, str]) -> str:
    """Swap whole profile names only: lab1, not the lab1 of pve-lab1."""
    for old in sorted(names, key=len, reverse=True):
        text = re.sub(rf"(?<![\w-]){re.escape(old)}(?![\w-])", names[old], text)
    return text


def _marked_html(text: Text, marks: list[str], width: int) -> str:
    """The capture as HTML, each mark's text wrapped with its number."""
    plain = text.plain
    for n, phrase in enumerate(marks, 1):
        at = plain.find(phrase)
        if at < 0:
            raise SystemExit(f"mark {n} '{phrase}' not found in the capture")
        text.stylize(Style(bgcolor=_MARK_BG.format(n)), at, at + len(phrase))
    console = Console(record=True, width=width, force_terminal=True, color_system="truecolor",
                      file=open(os.devnull, "w"))
    console.print(text)
    body = console.export_html(theme=THEME, inline_styles=True, code_format="{code}").rstrip()
    seen: set[str] = set()

    def tag(m: re.Match[str]) -> str:
        n = str(int(m.group(2), 16))
        style = (m.group(1) + m.group(3)).strip("; ")
        first = n not in seen
        seen.add(n)
        cls = "mark mark-first" if first else "mark"
        return f'<span class="{cls}" data-n="{n}"' + (f' style="{style}"' if style else "") + ">"

    return re.sub(r'<span style="([^"]*?)background-color: #fe00(\w\w);?([^"]*)">', tag, body)


def take(shot: Shot, conf: dict) -> Path:
    names = doc_names(conf)
    args = [_rename_profiles(a.replace("{profile}", conf["profile"]), names) for a in shot.args]
    raw = run_pvecli(args, shot.stdin, shot.width, shot.cwd)
    for prompt, answer in shot.echo:
        raw = re.sub(_ansi_tolerant(prompt), lambda m, a=answer: f"{m.group(0)}{a}\n", raw, count=1)
    raw = raw.rstrip("\n")

    text = Text.from_ansi(raw)
    leaks = [p for p in conf.get("forbid", []) if re.search(p, text.plain, flags=re.I)]
    if leaks:
        raise SystemExit(f"{shot.name}: not anonymized, still matches {leaks}")

    shown = html.escape(shell_line(args))
    body = _marked_html(text, shot.marks, shot.width)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{shot.name}.html"
    path.write_text(
        f'<figure class="terminal{" terminal--wide" if shot.width > WIDTH else ""}" markdown="0">'
        f'<pre class="terminal-body"><span class="terminal-prompt">$ </span>'
        f'<span class="terminal-command">pvecli {shown}</span>\n{body}</pre></figure>\n'
    )
    return path


def shell_line(args: list[str]) -> str:
    """The arguments as typed in a shell: quoted only when needed, ~ left bare."""
    return " ".join(a if re.fullmatch(r"[\w@%+=:,./~-]+", a) else shlex.quote(a) for a in args)


def load_conf() -> dict:
    if not LOCAL.exists():
        raise SystemExit(f"Missing {LOCAL.relative_to(ROOT)}: copy screens.example.toml and fill it in")
    return tomllib.loads(LOCAL.read_text())


def main() -> int:
    conf = load_conf()
    wanted = set(sys.argv[1:])
    SHOTS = _load_shots()
    unknown = wanted - {s.name for s in SHOTS}
    if unknown:
        raise SystemExit(f"Unknown shot(s): {', '.join(sorted(unknown))}")
    for old in OUT.glob("*.html"):
        if not wanted and old.stem not in {s.name for s in SHOTS}:
            old.unlink()
    for shot in SHOTS:
        if (wanted and shot.name not in wanted) or (not wanted and shot.manual):
            continue
        print(f"{shot.name}: {take(shot, conf).relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
