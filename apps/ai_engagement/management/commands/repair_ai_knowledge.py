"""Revision-bound, explicit source repair; no customer-triggered reindexing."""
import json
from uuid import UUID
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Plan knowledge repair, or --apply an exact fingerprint (may use AI credits). No messages are sent."

    def add_arguments(self, parser):
        parser.add_argument("--organization-id", type=UUID, required=True)
        parser.add_argument("--document-id", type=int)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--expected-fingerprint")
        parser.add_argument("--request-id", type=UUID, help="Inspect an existing request.")
        parser.add_argument("--dispatch", action="store_true", help="Redispatch a queued/broker-failed request with the same task ID.")

    def handle(self, *args, **options):
        from django.db import DatabaseError
        from apps.organizations.models import Organization
        from apps.ai_engagement.models import KnowledgeRepairRequest
        from apps.ai_engagement.services.knowledge_repair import KnowledgeRepairError, plan_repair, request_repair, dispatch_repair
        if bool(options["document_id"]) == bool(options["request_id"]):
            raise CommandError("Choose exactly one --document-id or --request-id.")
        if options["dispatch"] and (not options["request_id"] or options["apply"]):
            raise CommandError("--dispatch requires --request-id and cannot be combined with --apply.")
        if options["apply"] and (not options["document_id"] or not options["expected_fingerprint"]):
            raise CommandError("--apply requires --document-id and its --expected-fingerprint.")
        try:
            organization = Organization.objects.filter(pk=options["organization_id"], is_active=True).first()
            if organization is None:
                raise KnowledgeRepairError("active_organization_not_found")
            if options["document_id"]:
                if not options["apply"]:
                    result = plan_repair(organization=organization, document_id=options["document_id"])
                    self.stdout.write(json.dumps(result))
                    return
                request = request_repair(organization=organization, document_id=options["document_id"],
                    expected_fingerprint=options["expected_fingerprint"])
            else:
                request = KnowledgeRepairRequest.objects.filter(pk=options["request_id"], organization=organization).first()
                if request is None:
                    raise KnowledgeRepairError("repair_request_not_found")
                if options["dispatch"]:
                    dispatch_repair(organization_id=organization.pk, request_id=request.pk)
            request.refresh_from_db()
            self.stdout.write(json.dumps({"request_id": str(request.pk), "status": request.status,
                "operation": request.operation, "attempt": request.attempt, "outcome_code": request.outcome_code,
                "document_id": request.document_id, "result_document_id": request.result_document_id}))
        except KnowledgeRepairError as exc:
            raise CommandError(str(exc)) from None
        except DatabaseError:
            raise CommandError("knowledge_repair_storage_error") from None
