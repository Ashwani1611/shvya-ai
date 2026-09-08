import json

from django.core.management.base import BaseCommand, CommandError

from apps.ai_engagement.services.diagnostics import diagnose_engagement
from apps.crm.models import Lead


class Command(BaseCommand):
    help = "Report WhatsApp AI blockers without sending messages or spending credits."

    def add_arguments(self, parser):
        parser.add_argument("--organization-id", required=True)
        parser.add_argument("--lead-id", required=True)

    def handle(self, *args, **options):
        lead = (
            Lead.objects.select_related("organization", "pipeline", "stage")
            .filter(organization_id=options["organization_id"], id=options["lead_id"])
            .first()
        )
        if lead is None:
            raise CommandError("Lead not found in this organization.")
        self.stdout.write(json.dumps(diagnose_engagement(lead=lead), indent=2))
