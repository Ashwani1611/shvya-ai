from collections import Counter
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from config.celery import app

REQUIRED = {
    "ai_realtime": {
        "ai.generate_ai_engagement_response",
        "apps.channels.tasks.send_whatsapp_message_task",
    },
    "hosted_ai": {
        "hosted.dispatch_due_ai",
        "apps.hosted_automation.tasks.process_hosted_ai_engagement_job_task",
    },
}


def missing_consumers(queues, registered):
    missing = []
    for queue, tasks in REQUIRED.items():
        if not any(
            queue in {item.get("name") for item in worker_queues}
            and tasks <= set(registered.get(worker, []))
            for worker, worker_queues in queues.items()
        ):
            missing.append(queue)
    return missing


def _execution_state(*, message):
    """Classify one inbound AI turn without exposing message or lead content."""
    from apps.channels.models import WhatsAppMessage

    payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    processed = bool((payload.get("shvya_ai_processing") or {}).get("processed"))
    ai_outbound = WhatsAppMessage.objects.filter(
        organization=message.organization,
        lead=message.lead,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        raw_payload__shvya_ai__source_inbound_message_id=str(message.id),
    ).order_by("-created_at", "-id").first()

    if ai_outbound is not None:
        if ai_outbound.status == WhatsAppMessage.Status.FAILED:
            return "ai_outbound_failed"
        if ai_outbound.status == WhatsAppMessage.Status.QUEUED:
            return "ai_outbound_still_queued"
        return "ai_outbound_created"
    if processed:
        # A processed source with no linked outbound is the explicit
        # should_engage=False path; engaging sends create the outbound in the
        # same transaction as this marker.
        return "processed_without_outbound_no_engagement"
    return "unprocessed_without_outbound"


def _credit_bucket(available):
    try:
        available = int(available)
    except (TypeError, ValueError):
        available = 0
    if available <= 0:
        return "credits_0"
    if available < 5:
        return "credits_1_to_4"
    if available < 20:
        return "credits_5_to_19"
    return "credits_20_plus"


def recent_api_ai_blockers(*, minutes=90, limit=25):
    """Return aggregate blocker/execution counts without customer data."""
    from apps.ai_engagement.services.diagnostics import diagnose_engagement
    from apps.channels.models import WhatsAppAccount, WhatsAppMessage

    recent = (
        WhatsAppMessage.objects.filter(
            direction=WhatsAppMessage.Direction.INBOUND,
            lead__isnull=False,
            account__connection_type=WhatsAppAccount.ConnectionType.API,
            created_at__gte=timezone.now() - timedelta(minutes=minutes),
        )
        .select_related("lead__organization", "lead__pipeline", "lead__stage")
        .order_by("-created_at", "-id")[: max(limit * 4, limit)]
    )

    seen_leads = set()
    blockers = Counter()
    inspected = 0
    for message in recent:
        lead = message.lead
        if lead is None or lead.id in seen_leads:
            continue
        seen_leads.add(lead.id)
        inspected += 1
        report = diagnose_engagement(lead=lead)
        report_blockers = report.get("blockers") or []
        if report_blockers:
            blockers.update(str(item) for item in report_blockers)
        else:
            blockers["ready"] += 1
        blockers[_execution_state(message=message)] += 1
        blockers[_credit_bucket(report.get("available_ai_credits"))] += 1
        if inspected >= limit:
            break

    return inspected, blockers


class Command(BaseCommand):
    help = "Verify AI queue consumers and summarize recent WhatsApp API blockers."

    def handle(self, *args, **options):
        inspect = app.control.inspect(timeout=5)
        missing = missing_consumers(
            inspect.active_queues() or {}, inspect.registered() or {}
        )
        if missing:
            raise CommandError("No ready AI consumer for: " + ", ".join(missing))

        self.stdout.write(
            self.style.SUCCESS("Hosted and WhatsApp API AI consumers are ready.")
        )

        inspected, blockers = recent_api_ai_blockers()
        if not inspected:
            self.stdout.write("Recent WhatsApp API AI diagnostics: no inbound leads found.")
            return

        summary = ", ".join(
            f"{reason}={count}" for reason, count in sorted(blockers.items())
        )
        self.stdout.write(
            f"Recent WhatsApp API AI diagnostics: leads={inspected}; {summary}"
        )
