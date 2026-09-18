"""Encrypted, persistent support files; plaintext is returned only by authorized views.

The existing web/worker MEDIA_ROOT mount is reused. Even an accidentally exposed
raw storage object contains authenticated ciphertext, never a customer upload.
Back up SECRET_KEY with the database and files. Preserve old keys through
SECRET_KEY_FALLBACKS until every existing attachment has been re-encrypted.
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

MAGIC = b"SHVYA-SUPPORT-ENCRYPTED-1\n"
MAX_PLAINTEXT = 100 * 1024 * 1024


def support_root():
    return Path(getattr(settings, "SUPPORT_PRIVATE_ROOT", "") or
                Path(settings.MEDIA_ROOT) / ".support-encrypted")


def file_cipher():
    secrets = [settings.SECRET_KEY, *getattr(settings, "SECRET_KEY_FALLBACKS", [])]
    return MultiFernet([Fernet(base64.urlsafe_b64encode(hmac.new(
        secret.encode("utf-8"), b"shvya-support-attachment-v1", hashlib.sha256
    ).digest())) for secret in secrets])


@deconstructible
class PrivateSupportStorage(FileSystemStorage):
    def __init__(self):
        super().__init__(location=support_root(), base_url=None,
                         file_permissions_mode=0o600, directory_permissions_mode=0o700)

    def _save(self, name, content):
        payload = content.read(MAX_PLAINTEXT + 1)
        if len(payload) > MAX_PLAINTEXT:
            raise SuspiciousFileOperation("Support attachment exceeds the storage limit.")
        encrypted = MAGIC + file_cipher().encrypt(payload)
        return super()._save(name, ContentFile(encrypted))

    def _open(self, name, mode="rb"):
        if mode not in {"r", "rb"}:
            raise ValueError("Support attachments are immutable.")
        with super()._open(name, "rb") as stored:
            # Bound the encrypted read even for a corrupted on-disk object.
            encrypted = stored.read(2 * MAX_PLAINTEXT + 1)
        if not encrypted.startswith(MAGIC) or len(encrypted) > 2 * MAX_PLAINTEXT:
            raise SuspiciousFileOperation("Invalid encrypted support attachment.")
        try:
            payload = file_cipher().decrypt(encrypted[len(MAGIC):])
        except InvalidToken as exc:
            raise SuspiciousFileOperation("Support attachment could not be verified.") from exc
        if len(payload) > MAX_PLAINTEXT:
            raise SuspiciousFileOperation("Support attachment exceeds the storage limit.")
        return ContentFile(payload, name=name)

    def size(self, name):
        with self._open(name) as content:
            return content.size

    def url(self, name):
        raise ValueError("Support files require an authorized download endpoint.")


def attachment_path(instance, filename):
    return f"{instance.message.ticket.organization_id}/{instance.message.ticket_id}/{uuid.uuid4().hex}"


private_storage = PrivateSupportStorage()
