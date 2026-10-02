"""Recover validated guided uploads without retrying paid knowledge indexing."""
from django.core.management.base import BaseCommand

from apps.ai_engagement.models import Document
from apps.ai_engagement.services.file_sharing import FileSharingError, FileSharingService


class Command(BaseCommand):
    help = "Validate and enable pending/failed guided uploads for file delivery."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        ready = failed = superseded = 0
        documents = Document.objects.filter(
            file_sharing_ready=False,
            processing_status__in=["pending", "processing", "failed"],
        ).exclude(file="").exclude(share_instruction="").order_by("pk")[:max(0, options["limit"])]
        for document in documents:
            # Never resurrect an old failed version after a newer upload.
            if document.source_key and Document.objects.filter(
                organization_id=document.organization_id, source_key=document.source_key,
                version__gt=document.version,
            ).exists():
                superseded += 1
                continue
            try:
                FileSharingService.prepare_uploaded_file(document=document)
            except FileSharingError:
                failed += 1
            else:
                ready += 1
        self.stdout.write(f"Guided file readiness: ready={ready}, invalid_or_missing={failed}, superseded={superseded}")
