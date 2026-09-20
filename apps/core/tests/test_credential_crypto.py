import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken
from django.test import SimpleTestCase, override_settings

from apps.core.crypto import credential_cipher


def _legacy_cipher(secret):
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _dedicated_cipher(secret, purpose):
    digest = hmac.new(
        secret.encode("utf-8"),
        purpose.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


class CredentialCipherTests(SimpleTestCase):
    @override_settings(
        SECRET_KEY="legacy-django-secret",
        SECRET_KEY_FALLBACKS=[],
        CREDENTIAL_ENCRYPTION_KEY="dedicated-credential-secret",
        CREDENTIAL_ENCRYPTION_KEY_FALLBACKS=[],
    )
    def test_legacy_secret_key_ciphertext_remains_readable(self):
        legacy = _legacy_cipher("legacy-django-secret")
        ciphertext = legacy.encrypt(b"existing-provider-token")

        self.assertEqual(
            credential_cipher(
                purpose="shvya-integrations-v1"
            ).decrypt(ciphertext),
            b"existing-provider-token",
        )

    @override_settings(
        SECRET_KEY="legacy-django-secret",
        SECRET_KEY_FALLBACKS=[],
        CREDENTIAL_ENCRYPTION_KEY="dedicated-credential-secret",
        CREDENTIAL_ENCRYPTION_KEY_FALLBACKS=[],
    )
    def test_new_ciphertext_uses_dedicated_key_not_legacy_secret_key(self):
        ciphertext = credential_cipher(
            purpose="shvya-integrations-v1"
        ).encrypt(b"new-provider-token")

        with self.assertRaises(InvalidToken):
            _legacy_cipher("legacy-django-secret").decrypt(ciphertext)

        self.assertEqual(
            credential_cipher(
                purpose="shvya-integrations-v1"
            ).decrypt(ciphertext),
            b"new-provider-token",
        )

    @override_settings(
        SECRET_KEY="legacy-django-secret",
        SECRET_KEY_FALLBACKS=[],
        CREDENTIAL_ENCRYPTION_KEY="new-primary-key",
        CREDENTIAL_ENCRYPTION_KEY_FALLBACKS=["previous-dedicated-key"],
    )
    def test_previous_dedicated_key_remains_decryptable_during_rotation(self):
        previous_ciphertext = _dedicated_cipher(
            "previous-dedicated-key",
            "shvya-channels-v1",
        ).encrypt(b"rotating-token")

        self.assertEqual(
            credential_cipher(
                purpose="shvya-channels-v1"
            ).decrypt(previous_ciphertext),
            b"rotating-token",
        )
