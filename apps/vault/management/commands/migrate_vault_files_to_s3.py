"""Copy legacy encrypted Vault attachments to the configured private S3 prefix.

Copies ciphertext directly (never decrypts it), verifies SHA-256, and leaves
local files intact for rollback. Run on the host with the persistent media mount.
"""
import hashlib

from django.core.management.base import BaseCommand, CommandError

from apps.vault.models import VaultEntry
from apps.vault.storage import private_storage


def digest(fileobj):
    h = hashlib.sha256()
    while True:
        chunk = fileobj.read(1024 * 1024)
        if not chunk:
            break
        h.update(chunk)
    return h.hexdigest()


class Command(BaseCommand):
    help = "Copy and verify existing encrypted Vault files in S3 without removing local originals."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Perform uploads (otherwise dry run)")
        parser.add_argument("--limit", type=int, default=0, help="Maximum file rows to inspect")

    def handle(self, *args, **opts):
        storage = private_storage
        remote = storage.s3_backend
        if remote is None:
            raise CommandError("Enable USE_S3_STORAGE with private S3 configuration first.")
        entries = VaultEntry.objects.exclude(file="").order_by("id")
        if opts["limit"] > 0:
            entries = entries[:opts["limit"]]
        migrated = already = missing = 0
        for entry in entries.iterator():
            name = entry.file.name
            if remote.exists(name):
                local_path = storage.path(name)
                if local_path and __import__("os").path.isfile(local_path):
                    with open(local_path, "rb") as original, remote.open(name, "rb") as uploaded:
                        if digest(original) != digest(uploaded):
                            raise CommandError(f"Remote ciphertext differs: {entry.pk}")
                already += 1
                continue
            local_path = storage.path(name)
            from pathlib import Path
            if not Path(local_path).is_file():
                missing += 1
                self.stderr.write(f"MISSING (not on S3 or local disk): {entry.pk}")
                continue
            if not opts["apply"]:
                self.stdout.write(f"WOULD COPY {entry.pk}: {name}")
                migrated += 1
                continue
            with open(local_path, "rb") as source:
                actual = remote.save(name, source)
            if actual != name:
                raise CommandError(f"S3 renamed object unexpectedly: {name} -> {actual}")
            with open(local_path, "rb") as source, remote.open(name, "rb") as stored:
                if digest(source) != digest(stored):
                    raise CommandError(f"Verification failed: {entry.pk}")
            migrated += 1
        self.stdout.write(
            f"Vault file migration: copied_or_planned={migrated} "
            f"already_present={already} missing={missing} dry_run={not opts['apply']}"
        )
        if missing:
            raise CommandError("Some Vault files were not located; investigate before cleanup.")
