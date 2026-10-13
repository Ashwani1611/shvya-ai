"""Open review tickets from recorded API evidence, without replaying operations."""
from django.core.management.base import BaseCommand

from apps.core.api_activity.health import discover_attention


class Command(BaseCommand):
    help = "Idempotently create API incident reviews from recent failed/overdue activities."

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=24)
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        try:
            result = discover_attention(hours=options["hours"], limit=options["limit"])
        except ValueError as exc:
            from django.core.management.base import CommandError
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(str(result)))
