"""Age encryption for sensitive config fields."""

from pathlib import Path

import pyrage

AGE_PREFIX = "AGE:"
_IDENTITY_FILE = Path.home() / ".config" / "pvecli" / ".age-identity"


def _ensure_keypair() -> tuple[pyrage.x25519.Identity, pyrage.x25519.Recipient]:
    """Load or generate age keypair."""
    if _IDENTITY_FILE.exists():
        identity = pyrage.x25519.Identity.from_str(_IDENTITY_FILE.read_text().strip())
    else:
        _IDENTITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        identity = pyrage.x25519.Identity.generate()
        _IDENTITY_FILE.write_text(str(identity))
        _IDENTITY_FILE.chmod(0o600)
    return identity, identity.to_public()


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
