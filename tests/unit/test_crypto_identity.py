"""The age identity: created once, never readable by others, race free."""

import stat
import threading
from concurrent.futures import ThreadPoolExecutor

import src.crypto as crypto


def test_parallel_first_use_agrees_on_one_identity(home):
    # Fleet mode on a fresh install: every thread finds no identity at once.
    # They must all end up with the same key, or a password encrypted by one
    # thread can no longer be decrypted (seen as "invalid Bech32 encoding")
    for _ in range(10):
        crypto._IDENTITY_FILE.unlink(missing_ok=True)
        barrier = threading.Barrier(16)

        def first_use(_, barrier=barrier):
            barrier.wait()
            return str(crypto._ensure_keypair()[0])

        with ThreadPoolExecutor(max_workers=16) as pool:
            keys = set(pool.map(first_use, range(16)))
        assert len(keys) == 1
        assert crypto._IDENTITY_FILE.read_text().strip() == keys.pop()


def test_identity_is_never_created_readable_by_others(home, monkeypatch):
    # Checked at the moment the secret is written, not after: write then
    # chmod leaves a window where the key is world readable
    modes = []
    real_write = crypto.os.write

    def spy(fd, data):
        modes.append(stat.S_IMODE(crypto.os.fstat(fd).st_mode))
        return real_write(fd, data)

    monkeypatch.setattr(crypto.os, "write", spy)
    crypto._IDENTITY_FILE.unlink(missing_ok=True)
    crypto._ensure_keypair()
    assert modes and all(m == 0o600 for m in modes)
    assert stat.S_IMODE(crypto._IDENTITY_FILE.stat().st_mode) == 0o600


def test_round_trip(home):
    assert crypto.decrypt(crypto.encrypt("s3cret")) == "s3cret"
