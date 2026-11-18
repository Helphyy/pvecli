"""Pure helpers of `pvecli backup`: vzdump logs, volids, prune options."""

from __future__ import annotations

import pytest

from src.cli.backup import (
    backup_kind,
    parse_vzdump_log,
    property_string,
    prune_spec,
    upid_start,
)
from tests.fake_api import fixture


def _lines(name: str) -> list[str]:
    return [x["t"] for x in fixture(f"pve-9.1/{name}")]


# ── vzdump logs (recorded on lab1) ──────────────────────────────────────


def test_recorded_log_with_one_success_and_one_failure():
    # vzdump 300 919: 300 saved, 919 does not exist
    out = parse_vzdump_log(_lines("backup_vzdump_log_multi_job_errors"))
    assert out[300] == ("ok", "")
    assert out[919] == ("failed", "unable to find VM '919'")
    assert set(out) == {300, 919}


def test_recorded_pbs_log_single_guest_ok():
    assert parse_vzdump_log(_lines("backup_vzdump_log_single_ok")) == {300: ("ok", "")}


def test_recorded_log_where_a_guest_on_another_node_is_silently_dropped():
    # vzdump 300 919 on pve-lab1, 300 being on pve-lab2: 300 is in no line
    # of the log at all, so its absence is what tells it was not saved
    out = parse_vzdump_log(_lines("backup_vzdump_log_external_guest_dropped"))
    assert out == {919: ("failed", "unable to find VM '919'")}


def test_started_but_never_finished_is_failed():
    out = parse_vzdump_log([
        "INFO: Starting Backup of VM 100 (qemu)",
        "INFO: creating vzdump archive '/x'",
        "TASK ERROR: interrupted by signal",
    ])
    assert out[100][0] == "failed"
    assert "interrupted by signal" in out[100][1]


def test_failure_after_finished_line_of_other_guest_is_kept_apart():
    out = parse_vzdump_log([
        "INFO: Starting Backup of VM 100 (qemu)",
        "INFO: Finished Backup of VM 100 (00:00:03)",
        "INFO: Starting Backup of VM 101 (qemu)",
        "ERROR: Backup of VM 101 failed - no space left on device",
        "INFO: Failed at 2026-11-18 15:12:13",
    ])
    assert out == {100: ("ok", ""), 101: ("failed", "no space left on device")}


# ── volids ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("volid,kind", [
    ("local:backup/vzdump-qemu-300-2026_11_18-15_12_13.vma.zst", "qemu"),
    ("local:backup/vzdump-lxc-310-2026_11_18-15_12_13.tar.zst", "lxc"),
    ("nfs:backup/vzdump-openvz-310-2016_01_01-00_00_00.tar.gz", "lxc"),
    ("pbs-lab:backup/vm/300/2026-08-03T21:32:41Z", "qemu"),
    ("pbs-lab:backup/ct/310/2026-08-03T21:32:41Z", "lxc"),
    ("local:iso/debian.iso", None),
    ("local:backup/something-else.tar", None),
])
def test_backup_kind(volid, kind):
    assert backup_kind(volid) == kind


def test_upid_start_reads_the_hex_start_time():
    assert upid_start("UPID:pve-lab2:000307C6:0024ABFC:6AFDB23D:vzdump::root@pam:") == 0x6AFDB23D
    assert upid_start("garbage") == 0


# ── prune options ───────────────────────────────────────────────────────


def test_prune_spec_keeps_only_given_options_in_order():
    assert prune_spec(last=3, hourly=None, daily=7, weekly=None, monthly=0, yearly=None) == \
        "keep-last=3,keep-daily=7,keep-monthly=0"


def test_prune_spec_none_when_nothing_given():
    assert prune_spec(last=None, hourly=None, daily=None, weekly=None, monthly=None, yearly=None) is None


def test_property_string_from_object_and_passthrough():
    assert property_string({"enabled": 1, "storage": "local-lvm"}) == "enabled=1,storage=local-lvm"
    assert property_string("max-workers=4") == "max-workers=4"
