"""Dry-run first; server operators explicitly approve exact metered repairs."""
import json
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError


class Command(BaseCommand):
    help = "Inspect or queue one exact AI Brain source repair through the canonical ingestion tasks."

    def add_arguments(self, parser):
        parser.add_argument("--organization-id", required=True, type=UUID)
        parser.add_argument("--document-id", type=int)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--allow-credits", action="store_true")
        parser.add_argument("--expected-fingerprint")
        parser.add_argument("--request-id", type=UUID, help="Read a repair outcome instead of planning another repair.")
        parser.add_argument("--reconcile", action="store_true", help="Verify already stored indexing; never rerun provider calls.")
        parser.add_argument("--redispatch", action="store_true", help="Retry broker dispatch of the SAME unclaimed repair ticket.")

    def handle(self, *args, **options):
        from apps.organizations.models import Organization
        from apps.ai_engagement.services.source_repair import (
            SourceRepairError, inspect_document, request_repair, repair_report, dispatch_repair, reconcile_request,
        )
        try:
            organization = Organization.objects.filter(pk=options["organization_id"]).first()
            if organization is None:
                raise SourceRepairError("organization_not_found")
            if options["request_id"]:
                if options["document_id"] or options["apply"]:
                    raise SourceRepairError("choose_request_or_document")
                report = repair_report(organization=organization, request_id=options["request_id"])
                if options["reconcile"]:
                    if options["redispatch"]:
                        raise SourceRepairError("choose_reconcile_or_redispatch")
                    report = reconcile_request(organization=organization, request_id=options["request_id"])
                if options["redispatch"]:
                    if not options["allow_credits"] or not organization.is_active:
                        raise SourceRepairError("redispatch_requires_credit_consent_and_active_organization")
                    if report["state"] not in {"queued", "dispatch_failed"}:
                        raise SourceRepairError("running_or_uncertain_repairs_must_not_be_replayed")
                    dispatch_repair(organization_id=organization.pk, request_id=options["request_id"])
                    report = repair_report(organization=organization, request_id=options["request_id"])
            else:
                if not options["document_id"] or options["document_id"] <= 0 or options["redispatch"] or options["reconcile"]:
                    raise SourceRepairError("positive_document_id_required")
                report = inspect_document(organization=organization, document_id=options["document_id"])
                if options["apply"]:
                    if not options["expected_fingerprint"]:
                        raise SourceRepairError("review_dry_run_and_supply_expected_fingerprint")
                    request = request_repair(organization=organization, document_id=options["document_id"],
                        expected_fingerprint=options["expected_fingerprint"], allow_credits=options["allow_credits"])
                    report = repair_report(organization=organization, request_id=request.pk)
        except SourceRepairError as exc:
            raise CommandError(str(exc)) from None
        except DatabaseError:
            raise CommandError("repair_storage_unavailable") from None
        self.stdout.write(json.dumps(report, sort_keys=True, indent=2, default=str))
