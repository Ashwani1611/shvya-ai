"""Keep draft bytes private even when the normal media bucket is publicly served.

The application serves decoded image bytes through stable, permission-checked
routes. Raw storage objects are authenticated ciphertext. The configured default
storage (S3 or local persistent media) remains the single storage destination.
"""
import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.base import ContentFile
from django.core.files.storage import Storage, default_storage
from django.utils.deconstruct import deconstructible

MAX_BYTES = 15 * 1024 * 1024
MAGIC = b"SHVYA-BLOG-IMAGE-1\n"


def cipher():
    keys = [settings.SECRET_KEY, *getattr(settings, "SECRET_KEY_FALLBACKS", [])]
    return MultiFernet([Fernet(base64.urlsafe_b64encode(hmac.new(
        key.encode(), b"shvya-blog-image-v1", hashlib.sha256
    ).digest())) for key in keys])


@deconstructible
class BlogImageStorage(Storage):
    def _save(self, name, content):
        data = content.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise SuspiciousFileOperation("Blog image exceeds storage limit.")
        return default_storage.save(name, ContentFile(MAGIC + cipher().encrypt(data)))

    def _open(self, name, mode="rb"):
        if mode not in {"r", "rb"}:
            raise ValueError("Blog images are immutable.")
        with default_storage.open(name, "rb") as stored:
            encrypted = stored.read(MAX_BYTES * 2 + 1)
        if len(encrypted) > MAX_BYTES * 2 or not encrypted.startswith(MAGIC):
            raise SuspiciousFileOperation("Invalid stored blog image.")
        try:
            data = cipher().decrypt(encrypted[len(MAGIC):])
        except InvalidToken as exc:
            raise SuspiciousFileOperation("Stored blog image failed validation.") from exc
        if len(data) > MAX_BYTES:
            raise SuspiciousFileOperation("Stored blog image exceeds size limit.")
        return ContentFile(data, name=name)

    def exists(self, name):
        return default_storage.exists(name)

    def delete(self, name):
        return default_storage.delete(name)

    def size(self, name):
        with self._open(name) as content:
            return content.size

    def url(self, name):
        raise ValueError("Blog image URLs must use the access-controlled media routes.")


blog_image_storage = BlogImageStorage()
