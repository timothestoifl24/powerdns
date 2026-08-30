"""Encryption for the provider secrets held in the database.

Client secrets, LDAP bind passwords and SAML private keys are configurable
through the web UI, which means they land in a table rather than in a file
only root can read. They are encrypted at rest so that a database dump, a
replica, or a backup does not hand them over in plaintext.

The key is derived from ``SECRET_KEY`` with HKDF and a fixed info string, so
there is no second secret to distribute. The consequence is that **rotating
SECRET_KEY makes existing provider secrets undecryptable**; the panel reports
that clearly rather than failing at sign-in time, and the affected provider
simply needs its secret entered again.
"""

from __future__ import annotations

import base64
import logging

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger(__name__)

#: Domain separation. Changing this invalidates every stored secret, so it is
#: deliberately not configurable.
_HKDF_INFO = b"pdnsadmin.auth-provider-secrets.v1"

#: Marks a value as ciphertext produced by this module. Lets a plaintext value
#: written by an older version, or by hand, be recognised rather than mangled.
_PREFIX = "enc:v1:"


class SecretDecryptionError(RuntimeError):
    """Stored ciphertext could not be read with the current SECRET_KEY."""


def _fernet(secret_key: str) -> Fernet:
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_HKDF_INFO,
    ).derive(secret_key.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt(value: str, secret_key: str) -> str:
    """Encrypt ``value``. An empty value stays empty -- "unset" is not a secret."""
    if not value:
        return ""
    token = _fernet(secret_key).encrypt(value.encode("utf-8"))
    return _PREFIX + token.decode("ascii")


def decrypt(stored: str, secret_key: str) -> str:
    """Decrypt a value produced by :func:`encrypt`.

    A value without the marker is returned unchanged: it was never encrypted,
    which is what a hand-edited row looks like.
    """
    if not stored:
        return ""
    if not stored.startswith(_PREFIX):
        return stored
    try:
        return _fernet(secret_key).decrypt(stored[len(_PREFIX) :].encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise SecretDecryptionError(
            "A stored provider secret could not be decrypted. This normally means "
            "SECRET_KEY changed since it was saved; re-enter the secret to fix it."
        ) from exc


def is_encrypted(stored: str) -> bool:
    return stored.startswith(_PREFIX)
