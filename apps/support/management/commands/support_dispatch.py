from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Process support auto-close/reminders and pending email; optionally poll the verified mailbox."

    def add_arguments(self, parser):
        parser.add_argument("--mailbox", action="store_true")

    def handle(self, *args, **options):
        from apps.support.jobs import deliver_pending, maintain_tickets
        self.stdout.write(str(maintain_tickets()))
        self.stdout.write(str(deliver_pending()))
        if options["mailbox"]:
            from apps.support.mail import poll_mailbox
            self.stdout.write(str(poll_mailbox()))
