"""Captures of the Getting started section (docs/start/), and of the home page.

Setup for the manual shots, on prod (VMIDs 880-889, removed when done):
  - VM 880 web-02 on pve1, full clone of 9000 (debian-13-cloud) on ceph-vm,
    1 GiB, DHCP on vnet1, running (no guest agent in the image).
  - containers 881 dns-01 and 882 mail-01 on pve1, debian-13-standard,
    256 MiB, rootfs 2 GB on ceph-vm, DHCP on vnet1, running.

The profile captures (`config add`, `config default`) need an empty pvecli
config in which saving works, which the shared runners never give (the lab
config already holds `prod`, and anon_run refuses to write). They run in a
throwaway HOME, are not in SHOTS or CASTS, and are taken with:

    python -m scripts.docs.shots.start
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

from scripts.docs.casts import ENTER, UP, Cast
from scripts.docs.screens import Shot

SHOTS = [
    Shot("home-health", ["cluster", "health", "-p", "{profile}"],
         "Home page: the health of the prod cluster",
         marks=[]),
    Shot("start-config-test", ["config", "test", "-p", "{profile}"],
         "A working profile: address, PVE version, API release",
         marks=["Connection successful to 'prod'"]),
    Shot("start-node-list", ["node", "list", "-p", "{profile}"],
         "The five nodes of prod with their load", width=120,
         marks=["pve5"]),
    Shot("start-cluster-status", ["cluster", "status", "-p", "{profile}"],
         "Cluster name, quorum and the nodes", marks=["Yes"]),
    Shot("start-vm-list", ["vm", "list", "-p", "{profile}"],
         "Every VM of the cluster, running ones with their usage", width=120,
         marks=["web-02"], manual=True),
    Shot("start-ct-list", ["ct", "list", "-p", "{profile}"],
         "Every container of the cluster", width=120,
         marks=["dns-01", "mail-01"], manual=True),
    Shot("start-vm-list-order", ["vm", "list", "-p", "{profile}", "-o", "memory"],
         "--order: the Memory column first, rows sorted by it", width=120,
         marks=["Memory"], manual=True),
    Shot("start-order-columns", ["vm", "list", "-p", "{profile}", "-o"],
         "--order without a column: the names it accepts",
         marks=["Available"]),
    Shot("start-vm-info", ["vm", "info", "880", "-p", "{profile}"],
         "One VM: state, resources, configuration, network",
         marks=["web-02"], manual=True),
]


def _typed(text: str, wait: float) -> list[tuple[float, str]]:
    """Keys typed one by one, like a person would, then Enter."""
    keys = [(wait, text[0])] if text else []
    keys += [(0.06, c) for c in text[1:]]
    return keys + [(0.4, ENTER)]


CASTS = [
    Cast("start-vm-info-menu", ["vm", "info", "-p", "{profile}"],
         # 880 is second from the end of the list: the menu wraps around.
         # The last pause waits for the panel (the agent query of 880 is slow).
         [(5.0, UP), (0.8, UP), (1.2, ENTER), (15.0, "")], rows=28, manual=True),
]

# ── Captures in a throwaway HOME (python -m scripts.docs.shots.start) ────

TOKEN_PROD = "8c4f2a1e-6b3d-4e7a-9f15-2d8c7b6a3e41"
TOKEN_STAGING = "3e9d7c52-1a4b-4f86-b2e0-7c5a9d1f6b38"

FRESH_CASTS = [
    Cast("start-config-add", ["config", "add"],
         _typed("prod", 1.5) + _typed("10.0.0.11", 0.8) + [(0.8, ENTER)]
         + _typed("pvecli@pve", 0.8) + [(1.5, ENTER)]
         + _typed("cli", 0.8) + _typed(TOKEN_PROD, 0.8) + _typed("n", 0.8)
         + [(0.8, ENTER), (0.8, ENTER), (2.0, ENTER)],
         rows=34),
]

# Run after the cast, before the shots: a second profile, made the default
FRESH_SETUP = [
    (["config", "add", "staging", "-ho", "10.1.0.11", "-po", "8006", "-us", "pvecli@pve",
      "-tn", "cli", "-tv", TOKEN_STAGING], "y\ny\n"),
]

FRESH_SHOTS = [
    Shot("start-config-default", ["config", "default", "prod"],
         "The default profile set back to prod",
         marks=["Default profile set to 'prod'"]),
]

_SELF = "scripts.docs.shots.start"


def _fresh_run(args: list[str], stdin: str, width: int, cwd: str = "") -> str:
    """screens.run_pvecli, without anon_run: the config of HOME is used as is."""
    from scripts.docs import screens

    env = {**os.environ, "FORCE_COLOR": "1", "COLUMNS": str(width), "TERM": "xterm-256color",
           "PYTHONUNBUFFERED": "1", "PYTHONPATH": str(screens.ROOT)}
    run = subprocess.run([sys.executable, "-m", _SELF, "--pvecli", *args], input=stdin,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         env=env, timeout=120, cwd=screens.ROOT / cwd)
    return screens._LIVE.sub("", run.stdout)


def fresh() -> int:
    from scripts.docs import casts, screens

    home = tempfile.mkdtemp(prefix="pvecli-docs-home-")
    os.environ["HOME"] = home
    conf = screens.load_conf()

    # casts.record execs anon_run in the child: run this module instead
    real_exec = os.execvpe

    def exec_fresh(file: str, argv: list[str], env: dict[str, str]) -> None:
        real_exec(file, [argv[0], "-m", _SELF, "--pvecli", *argv[3:]], env)

    os.execvpe = exec_fresh  # type: ignore[assignment]
    for cast in FRESH_CASTS:
        print(f"{cast.name}: {casts.record(cast, conf).relative_to(screens.ROOT)}")
    os.execvpe = real_exec  # type: ignore[assignment]

    for args, stdin in FRESH_SETUP:
        _fresh_run(args, stdin, 80)
    screens.run_pvecli = _fresh_run  # type: ignore[assignment]
    for shot in FRESH_SHOTS:
        print(f"{shot.name}: {screens.take(shot, conf).relative_to(screens.ROOT)}")
    shutil.rmtree(home, ignore_errors=True)
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--pvecli"]:
        from src.cli.main import _pad_order_option, app

        app(args=_pad_order_option(sys.argv[2:]), prog_name="pvecli")
    else:
        sys.exit(fresh())
