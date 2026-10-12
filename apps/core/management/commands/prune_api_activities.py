from django.core.management.base import BaseCommand, CommandError

from apps.core.api_activity.retention import prune_activities


class Command(BaseCommand):
    help = "Prune expired API activity metadata in bounded batches (and linked events)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=None)
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument("--max-batches", type=int, default=20)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        try:
            result = prune_activities(
                days=options["days"], batch_size=options["batch_size"],
                max_batches=options["max_batches"], dry_run=options["dry_run"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        days = result["days"]
        if options["dry_run"]:
            self.stdout.write(f"Dry run: {result['eligible']} activities older than {days} days; no changes.")
            return
        self.stdout.write(f"Deleted {result['deleted']} activities older than {days} days (linked events included).")
        if result.get("has_more"):
            self.stdout.write("Expired activity remains; run another bounded batch or review cleanup capacity.")
