"""Password hashing and the MVP password policy.

Hashing goes through `pwdlib` configured with Argon2id only -- the approved
"pwdlib[argon2]" dependency for this phase. Never hash, log, or persist a
plaintext password anywhere else in this module.
"""

from __future__ import annotations

from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

#: MVP password policy: length-focused, no composition rules. Per NIST
#: 800-63B guidance, forcing a mix of upper/lower/digit/symbol classes
#: pushes users toward predictable substitutions ("Password1!") without
#: meaningfully raising entropy; a longer minimum length is the more
#: effective, simpler-to-explain control for an MVP. 10 characters is a
#: deliberately modest floor (not the 12+ some guidance recommends) chosen
#: to keep local development/demo account setup painless during the
#: hackathon; revisit before any real deployment.
MIN_PASSWORD_LENGTH = 10
#: Upper bound is a DoS guard, not a policy: Argon2id's cost is
#: proportional to input size, so an unbounded password could be used to
#: burn CPU/memory on every hash/verify call.
MAX_PASSWORD_LENGTH = 256

_password_hash = PasswordHash((Argon2Hasher(),))


def hash_password(plain_password: str) -> str:
    """Hash a plaintext password with Argon2id. Never call this on anything but a fresh password."""
    return _password_hash.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Constant-time-safe verification (delegated to `pwdlib`/`argon2`) against a stored hash."""
    return _password_hash.verify(plain_password, password_hash)


def is_argon2id_hash(password_hash: str) -> bool:
    """True if `password_hash` was produced by the Argon2id hasher this module configures.

    Used only by tests to prove the stored format is what we think it is --
    never used to gate authentication logic itself.
    """
    return password_hash.startswith("$argon2id$")
