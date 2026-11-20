"""Captures of the Reference section (docs/reference/).

Read-only: no test guest, nothing written on the lab.
"""

from scripts.docs.screens import Shot

SHOTS = [
    Shot("reference-fleet-failure",
         ["backup", "list", "--vmid", "300", "--storage", "pbs", "-p", "{profile},staging"],
         "A two-profile run where staging fails: its error under its header, then the "
         "failed-profiles line on stderr (exit code 1)",
         marks=["Failed on 1/2 profile(s): staging"], width=100),
    Shot("reference-fleet-failure-json",
         ["backup", "list", "--vmid", "300", "--storage", "pbs", "-p", "{profile},staging", "--json"],
         "The same run with --json: the failed profile holds its error and exit code",
         marks=['"exit_code": 1'], width=100),
]
