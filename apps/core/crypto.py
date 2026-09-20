"""Shared versioned encryption helpers for recoverable platform credentials."""

import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings


def _fernet_from_secret(secret, *, purpose=None):
    raw = str(secret or "").encode("utf-8")
    if purpose:
        digest = hmac.new(raw, purpose.encode("utf-8"), hashlib.sha256).digest()
    else:
        # Legacy SHVYA credential encryption used plain SHA-256(SECRET_KEY).
        digest = hashlib.sha256(raw).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def credential_cipher(*, purpose):
    """Return a rotating MultiFernet keyring for recoverable credentials.

    New ciphertext uses CREDENTIAL_ENCRYPTION_KEY when configured. Decryption
    also accepts any dedicated fallback keys and the historical SECRET_KEY
    derivation so existing database rows remain readable during migration.
    """
    primary = str(
        getattr(settings, "CREDENTIAL_ENCRYPTION_KEY", "") or ""
    ).strip()
    fallbacks = [
        str(value).strip()
        for value in getattr(
            settings,
            "CREDENTIAL_ENCRYPTION_KEY_FALLBACKS",
            [],
        )
        if str(value).strip()
    ]

    ciphers = []
    if primary:
        ciphers.append(_fernet_from_secret(primary, purpose=purpose))
    ciphers.extend(
        _fernet_from_secret(secret, purpose=purpose)
        for secret in fallbacks
    )

    # Backward compatibility for all ciphertext written before the dedicated
    # credential-key setting existed.
    legacy_secrets = [
        settings.SECRET_KEY,
        *getattr(settings, "SECRET_KEY_FALLBACKS", []),
    ]
    ciphers.extend(
        _fernet_from_secret(secret)
        for secret in legacy_secrets
        if str(secret or "")
    )

    return MultiFernet(ciphers)
