"""Encrypted persistent Vault files, decrypted only by authorized download views.

Back up SECRET_KEY with the database and media files. Preserve old keys in
SECRET_KEY_FALLBACKS until existing files have been re-encrypted. The existing
persistent media mount is safe to reuse: raw files contain ciphertext only.
"""
import base64
import hashlib
import hmac
import uuid
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible

MAGIC = b"SHVYA-VAULT-ENCRYPTED-1\n"
MAX_PLAINTEXT = 50 * 1024 * 1024


def vault_root():
    return Path(getattr(settings, "VAULT_STORAGE_ROOT", "") or Path(settings.MEDIA_ROOT) / ".vault-encrypted")


def file_cipher():
    keys = [settings.SECRET_KEY, *getattr(settings, "SECRET_KEY_FALLBACKS", [])]
    return MultiFernet([Fernet(base64.urlsafe_b64encode(hmac.new(
        key.encode("utf-8"), b"shvya-vault-attachment-v1", hashlib.sha256
    ).digest())) for key in keys])


@deconstructible
class VaultPrivateStorage(FileSystemStorage):
    def __init__(self):
        super().__init__(base_url=None, file_permissions_mode=0o600, directory_permissions_mode=0o700)

    @property
    def base_location(self):
        return str(vault_root())

    @property
    def location(self):
        return str(vault_root().resolve())

    def _save(self, name, content):
        payload = content.read(MAX_PLAINTEXT + 1)
        if len(payload) > MAX_PLAINTEXT:
            raise SuspiciousFileOperation("Vault file exceeds the storage limit.")
        return super()._save(name, ContentFile(MAGIC + file_cipher().encrypt(payload)))

    def _open(self, name, mode="rb"):
        if mode not in {"r", "rb"}:
            raise ValueError("Vault files are immutable.")
        with super()._open(name, "rb") as stored:
            encrypted = stored.read(2 * MAX_PLAINTEXT + 1)
        if not encrypted.startswith(MAGIC) or len(encrypted) > 2 * MAX_PLAINTEXT:
            raise SuspiciousFileOperation("Invalid encrypted Vault file.")
        try:
            payload = file_cipher().decrypt(encrypted[len(MAGIC):])
        except InvalidToken as exc:
            raise SuspiciousFileOperation("Vault file could not be verified.") from exc
        if len(payload) > MAX_PLAINTEXT:
            raise SuspiciousFileOperation("Vault file exceeds the storage limit.")
        return ContentFile(payload, name=name)

    def size(self, name):
        with self._open(name) as content:
            return content.size

    def url(self, name):
        raise ValueError("Vault files require an authorized download endpoint.")


def vault_upload_path(instance, filename):
    return f"{instance.vault_id}/{uuid.uuid4().hex}"


private_storage = VaultPrivateStorage()
