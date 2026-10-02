"""Read-only preflight by default; explicitly opted-in, no-send Sandbox replay."""
import json
import os
from pathlib import Path
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from apps.ai_engagement.services.recovery_scenarios import RecoveryEvaluationError


MAX_FIXTURE_BYTES = 128 * 1024


class Command(BaseCommand):
    help = "Inspect AI recovery readiness, or compare Sandbox OFF/ON with --live (uses AI credits; never sends)."

    def add_arguments(self, parser):
        parser.add_argument("--organization-id", required=True, type=UUID)
        parser.add_argument("--scenarios", help="Local UTF-8 JSON fixtures; no URLs or customer exports are fetched.")
        parser.add_argument("--live", action="store_true", help="Explicitly allow metered live AI calls in Sandbox.")
        parser.add_argument("--max-turns", type=int, default=8, help="Combined OFF/ON turns; hard maximum 40.")
        parser.add_argument("--budget-seconds", type=float, default=120.0)
        parser.add_argument("--output", help="Create a new, private JSON report; existing files are not overwritten.")

    def handle(self, *args, **options):
        from apps.organizations.models import Organization
        from apps.ai_engagement.services.recovery_evaluation import evaluate, preflight

        if options["scenarios"] and not options["live"]:
            raise CommandError("Use --live to authorize model/credit calls, or omit --scenarios for read-only preflight.")
        if options["live"] and not options["scenarios"]:
            raise CommandError("Live comparison requires an explicit --scenarios file.")
        output = options.get("output")
        if output and Path(output).exists():
            raise CommandError("output_already_exists")
        try:
            organization = Organization.objects.filter(pk=options["organization_id"]).first()
        except DatabaseError:
            raise CommandError("organization_storage_error") from None
        if organization is None:
            raise CommandError("organization_not_found")
        if not options["live"]:
            try:
                report = preflight(organization)
            except DatabaseError:
                raise CommandError("preflight_storage_error") from None
        else:
            try:
                with Path(options["scenarios"]).open("rb") as source:
                    data = source.read(MAX_FIXTURE_BYTES + 1)
                if len(data) > MAX_FIXTURE_BYTES:
                    raise RecoveryEvaluationError("fixture_too_large")
                payload = json.loads(data.decode("utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                raise CommandError("fixture_unreadable_or_invalid_json") from None
            except RecoveryEvaluationError as exc:
                raise CommandError(str(exc)) from None
            try:
                report = evaluate(organization, payload, max_turns=options["max_turns"],
                                  budget_seconds=options["budget_seconds"])
            except RecoveryEvaluationError as exc:
                raise CommandError(str(exc)) from None
            except DatabaseError:
                raise CommandError("comparison_storage_error") from None
        encoded = json.dumps(report, ensure_ascii=False, indent=2)
        if output:
            try:
                fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as destination:
                    destination.write(encoded + "\n")
            except OSError:
                raise CommandError("report_could_not_be_created") from None
        self.stdout.write(encoded)
        if options["live"] and (not report.get("comparison_valid")
                or (report.get("acceptance", {}).get("recovery", {}).get("passed") is not True)):
            raise CommandError("comparison_incomplete_or_not_comparable; inspect the redacted report")
