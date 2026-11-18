"""Age encryption for sensitive config fields."""

import os
import tempfile
from pathlib import Path

import pyrage  # type: ignore[import-untyped]  # pyrage ships no stubs nor py.typed, no types-pyrage package exists

AGE_PREFIX = "AGE:"
_IDENTITY_FILE = Path.home() / ".config" / "pvecli" / ".age-identity"


def _ensure_keypair() -> tuple[pyrage.x25519.Identity, pyrage.x25519.Recipient]:
    """Load or generate age keypair."""
    if not _IDENTITY_FILE.exists():
        _create_identity()
    identity = pyrage.x25519.Identity.from_str(_IDENTITY_FILE.read_text().strip())
    return identity, identity.to_public()


def _create_identity() -> None:
    """Write a new identity, unless another thread or process wins the race.

    The key goes to a temporary file created 0600 (never readable by
    others, not even for an instant), then os.link publishes it whole and
    only if no identity exists yet: two first uses at once (fleet mode on a
    fresh install) end up with the same key instead of overwriting each
    other, and nobody reads a half written file.
    """
    _IDENTITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=_IDENTITY_FILE.parent, prefix=".age-identity.", suffix=".tmp")
    try:
        os.write(fd, str(pyrage.x25519.Identity.generate()).encode())
        os.fsync(fd)
        os.close(fd)
        try:
            os.link(tmp, _IDENTITY_FILE)
        except FileExistsError:
            pass  # another first use won: its key is the one
    finally:
        os.unlink(tmp)


def encrypt(value: str) -> str:
    """Encrypt a plaintext value. Returns AGE:base64... string."""
    if value.startswith(AGE_PREFIX):
        return value
    _, recipient = _ensure_keypair()
    encrypted = pyrage.encrypt(value.encode(), [recipient])
    import base64
    return AGE_PREFIX + base64.b64encode(encrypted).decode()


def decrypt(value: str) -> str:
    """Decrypt an AGE:-prefixed value. Returns plaintext."""
    if not value.startswith(AGE_PREFIX):
        return value
    identity, _ = _ensure_keypair()
    import base64
    raw = base64.b64decode(value[len(AGE_PREFIX):])
    return pyrage.decrypt(raw, [identity]).decode()


def is_encrypted(value: str) -> bool:
    """Check if a value is age-encrypted."""
    return value.startswith(AGE_PREFIX)
