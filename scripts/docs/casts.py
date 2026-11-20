"""Animated recordings of pvecli's interactive menus, for the documentation.

Each cast runs pvecli in a real pseudo-terminal against a real cluster (API
answers anonymized, see anon_run.py), types the keys of its script, and
saves an asciicast v2 file (docs/assets/casts/NAME.cast) that pages play
with asciinema-player:

    <div class="cast" data-src="../../assets/casts/vm-info.cast"></div>

    python -m scripts.docs.casts            # every cast
    python -m scripts.docs.casts vm-info    # some casts
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import struct
import sys
import termios
import time
from dataclasses import dataclass
from pathlib import Path

from scripts.docs.screens import LOCAL, ROOT, _rename_profiles, doc_names, load_conf, shell_line

OUT = ROOT / "docs" / "assets" / "casts"
COLS, ROWS = 80, 22

# simple_term_menu reads the arrow keys of terminfo: application mode for
# xterm-256color (kcuu1/kcud1), not the plain CSI arrows
UP, DOWN, ENTER, SPACE = "\x1bOA", "\x1bOB", "\r", " "


@dataclass
class Cast:
    name: str
    args: list[str]                  # pvecli arguments, {profile} replaced
    keys: list[tuple[float, str]]    # (seconds to wait first, keys to type)
    rows: int = ROWS
    manual: bool = False             # changes the lab or needs a setup: only run by name


def _load_casts() -> list[Cast]:
    """Every recording, from scripts/docs/shots/*.py (one file per doc section)."""
    import importlib
    import pkgutil

    from scripts.docs import shots as package

    found: list[Cast] = []
    for info in pkgutil.iter_modules(package.__path__):
        found += getattr(importlib.import_module(f"scripts.docs.shots.{info.name}"), "CASTS", [])
    return found


def _type(fd: int, text: str) -> None:
    os.write(fd, text.encode())


def record(cast: Cast, conf: dict) -> Path:
    names = doc_names(conf)
    args = [_rename_profiles(a.replace("{profile}", conf["profile"]), names) for a in cast.args]
    env = {**os.environ, "TERM": "xterm-256color", "COLUMNS": str(COLS), "LINES": str(cast.rows),
           "PVECLI_DOCS_ANON": str(LOCAL)}
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(ROOT)
        os.execvpe(sys.executable, [sys.executable, "-m", "scripts.docs.anon_run", *args], env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", cast.rows, COLS, 0, 0))

    events: list[tuple[float, str]] = []
    start = time.monotonic()
    shown = shell_line(args)
    # The prompt, then the command line typed like a person would
    events.append((0.1, "\x1b[1;38;2;229;112;0m$ \x1b[0m\x1b[1m"))
    for i, ch in enumerate(f"pvecli {shown}"):
        events.append((0.3 + i * 0.045, ch))
    events.append((0.3 + len(shown) * 0.045 + 0.6, "\x1b[0m"))
    events.append((0.3 + len(shown) * 0.045 + 0.7, "\r\n"))
    offset = events[-1][0] + 0.2

    pending = list(cast.keys)
    next_key = time.monotonic() + (pending[0][0] if pending else 0)
    done = False
    while not done:
        timeout = max(0.0, next_key - time.monotonic()) if pending else 5.0
        ready, _, _ = select.select([fd], [], [], timeout)
        if ready:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if not data:
                done = True
                break
            events.append((offset + time.monotonic() - start, data.decode(errors="replace")))
        elif pending:
            _type(fd, pending.pop(0)[1])
            next_key = time.monotonic() + (pending[0][0] if pending else 0)
        else:
            done = True
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass

    text = "".join(e[1] for e in events)
    plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    leaks = [p for p in conf.get("forbid", []) if re.search(p, plain, flags=re.I)]
    if leaks:
        raise SystemExit(f"{cast.name}: not anonymized, still matches {leaks}")

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{cast.name}.cast"
    header = {"version": 2, "width": COLS, "height": cast.rows, "env": {"TERM": "xterm-256color"}}
    lines = [json.dumps(header)]
    lines += [json.dumps([round(t, 3), "o", d]) for t, d in events]
    lines.append(json.dumps([round(events[-1][0] + 2.5, 3), "o", ""]))  # hold the last screen
    path.write_text("\n".join(lines) + "\n")
    return path


def main() -> int:
    conf = load_conf()
    wanted = set(sys.argv[1:])
    for cast in _load_casts():
        if (wanted and cast.name not in wanted) or (not wanted and cast.manual):
            continue
        print(f"{cast.name}: {record(cast, conf).relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
