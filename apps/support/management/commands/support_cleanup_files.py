"""Remove old private orphan files. Dry run by default; never follow symlinks."""
from datetime import timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from apps.support.models import Attachment
from apps.support.storage import private_storage


class Command(BaseCommand):
    help = "List orphan support uploads older than 48 hours; pass --delete after reviewing."

    def add_arguments(self, parser):
        parser.add_argument("--delete", action="store_true")
        parser.add_argument("--older-than-hours", type=int, default=48)
        parser.add_argument("--limit", type=int, default=1000)

    def handle(self, *args, **options):
        hours = options["older_than_hours"]
        if hours < 24 or not 1 <= options["limit"] <= 10000:
            raise CommandError("Use at least 24 grace hours and a limit between 1 and 10000.")
        root = Path(private_storage.location).resolve()
        cutoff = (timezone.now() - timedelta(hours=hours)).timestamp()
        if not root.exists():
            self.stdout.write("No private support directory exists.")
            return
        found = 0
        # Filesystem scan is intentionally opt-in maintenance, never a request path.
        for path in root.rglob("*"):
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                continue
            if path.stat().st_mtime >= cutoff:
                continue
            name = path.relative_to(root).as_posix()
            if Attachment.objects.filter(file=name).exists():
                continue
            self.stdout.write(name)
            if options["delete"]:
                private_storage.delete(name)
            found += 1
            if found >= options["limit"]:
                break
        self.stdout.write(f"{'Deleted' if options['delete'] else 'Found'} {found} old private orphan file(s).")
