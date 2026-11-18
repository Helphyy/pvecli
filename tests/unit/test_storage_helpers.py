"""Helpers of src/cli/storage.py: storage ID rule, PBS fingerprint pickup and
scan entry keys.

Protects the storage ID check, which must refuse exactly what PVE refuses
(pve_verify_storage_id), so a bad ID fails before any scan.
"""

from __future__ import annotations

import pytest

from src.cli.storage import _FINGERPRINT_RE, _STORAGE_ID_RE, _scan_key
from tests.fake_api import fixture


@pytest.mark.parametrize("sid", [
    "ab", "nfs1", "local-lvm", "backup_pbs", "ceph.rbd", "NAS01", "a-1", "x.y_z-9",
])
def test_storage_id_accepted(sid):
    assert _STORAGE_ID_RE.match(sid)


@pytest.mark.parametrize("sid", [
    "a",            # one character
    "1nfs",         # starts with a digit
    "-nfs",
    "_nfs",
    "nfs-",         # ends with a separator
    "nfs_",
    "nfs.",
    "my nfs",       # space
    "nfs/1",
    "nfs:1",
    "",
])
def test_storage_id_refused(sid):
    assert not _STORAGE_ID_RE.match(sid)


def test_real_storage_ids_are_valid():
    for entry in fixture("pve-9.1/storage") + fixture("pve-9.2/storage"):
        assert _STORAGE_ID_RE.match(entry["storage"]), entry["storage"]


SHA256_FP = fixture("pve-9.2/nodes")[0]["ssl_fingerprint"]


def test_fingerprint_found_in_an_error():
    err = (f"error fetching datastores - fingerprint '{SHA256_FP.lower()}' not verified, "
           "abort! (500)")
    assert _FINGERPRINT_RE.findall(err) == [SHA256_FP.lower()]
    assert _FINGERPRINT_RE.findall(f"x {SHA256_FP} y") == [SHA256_FP]


def test_fingerprint_needs_32_bytes():
    # A SHA-1 fingerprint (20 bytes) or a truncated SHA-256 is not picked up
    sha1 = ":".join(["ab"] * 20)
    truncated = ":".join(SHA256_FP.split(":")[:31])
    assert _FINGERPRINT_RE.findall(sha1) == []
    assert _FINGERPRINT_RE.findall(truncated) == []
    assert _FINGERPRINT_RE.findall("zz:" * 31 + "zz") == []


@pytest.mark.parametrize("scan, entry, key", [
    ("nfs", {"path": "/export/vm", "options": "10.0.0.0/24(rw)"}, "/export/vm"),
    ("cifs", {"share": "backups", "description": "Backups"}, "backups"),
    ("iscsi", {"target": "iqn.2003-01.org.linux-iscsi:sn.1", "portal": "10.0.0.5:3260"},
     "iqn.2003-01.org.linux-iscsi:sn.1"),
    ("pbs", {"store": "datastore1", "comment": ""}, "datastore1"),
    ("zfs", {"pool": "tank"}, "tank"),
    ("lvm", {"vg": "pve"}, "pve"),
    ("other", {"name": "x"}, "x"),
    ("nfs", {}, ""),
    ("other", {}, ""),
])
def test_scan_key(scan, entry, key):
    assert _scan_key(scan, entry) == key


def test_scan_key_does_not_mix_backends():
    # An NFS entry is keyed by its path, never by another field
    assert _scan_key("nfs", {"share": "s", "store": "d"}) == ""
    assert _scan_key("pbs", {"path": "/p", "store": "d1"}) == "d1"
