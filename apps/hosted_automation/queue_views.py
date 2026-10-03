from datetime import datetime, timedelta, timezone as datetime_timezone

from django.db.models import Case, CharField, Exists, IntegerField, OuterRef, Q, Value, When
from django.db.models.fields.json import KeyTextTransform, KeyTransform
from django.db.models.functions import Cast, Replace
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.decorators import crm_login_required
from apps.followups.models import AutoFollowupSettings, LeadSequenceState
from apps.hosted_automation.models import HostedAutomationJob, HostedFollowupStepConfig
from apps.organizations.features import is_hosted_account_enabled
from services.channels.hosted_automation_service import (
    HOSTED_AI_PROCESSING_STALE_SECONDS,
    automation_pause_until,
    job_ai_block_reason,
)
from services.channels.ai_send_gate import AI_SEND_GAP_SECONDS, next_ai_send_at
from services.channels.hosted_whatsapp_service import account_ai_block_reason, get_session_settings


_FAR_FUTURE = datetime.max.replace(tzinfo=datetime_timezone.utc)


def _iso(value):
    return value.isoformat() if value else ""


def _latest_time(*values):
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _reason_label(reason):
    return str(reason or "").replace("_", " ").strip().title()


def _queue_sort_key(item):
    raw = item.get("available_at") or ""
    if raw:
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            when = _FAR_FUTURE
    else:
        when = _FAR_FUTURE
    # AI preserves the dispatcher's welcome/FIFO head even during backoff;
    # availability is eligibility, not a promise that a later job can overtake.
    priority = int(item.get("priority") or 9)
    created = item.get("created_at") or ""
    if item.get("effective_status") == "processing":
        return (-1, priority, created)
    if priority <= 2:
        return (0, priority, created)
    return (1, when, priority, created)


def _unrepresented_queued_messages(account, jobs):
    """Show legacy AI/bump-up rows without duplicating a durable queue job."""
    def uuid_text(expression):
        # UUID columns render with different separators across database engines.
        return Replace(Cast(expression, CharField()), Value("-"), Value(""))

    ai = KeyTransform("shvya_ai", "raw_payload")
    welcome = KeyTransform("shvya_welcome", "raw_payload")
    references = jobs.annotate(
        _source_ref=uuid_text("source_message_id"),
        _job_ref=uuid_text("id"),
        _outbound_ref=uuid_text(KeyTextTransform("message_id", "result")),
    )
    return WhatsAppMessage.objects.filter(
        organization=account.organization, account=account,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        status__in=[WhatsAppMessage.Status.QUEUED, "sending"],
    ).exclude(raw_payload__has_key="shvya_auto_followup").annotate(
        _source_ref=uuid_text(KeyTextTransform("source_inbound_message_id", ai)),
        _ai_job_ref=uuid_text(KeyTextTransform("job_id", ai)),
        _welcome_job_ref=uuid_text(KeyTextTransform("job_id", welcome)),
        _message_ref=uuid_text("id"),
    ).annotate(
        _represented=Exists(references.filter(
            Q(_source_ref=OuterRef("_source_ref"))
            | Q(_job_ref=OuterRef("_ai_job_ref"))
            | Q(_job_ref=OuterRef("_welcome_job_ref"))
            | Q(_outbound_ref=OuterRef("_message_ref"))
        )),
        _welcome_represented=Exists(jobs.filter(
            kind=HostedAutomationJob.Kind.WELCOME, lead_id=OuterRef("lead_id"),
        )),
    ).exclude(_represented=True).exclude(
        Q(raw_payload__has_key="shvya_welcome", _welcome_represented=True)
    )


@crm_login_required
@require_GET
def hosted_session_queue_view(request, account_id):
    if not is_hosted_account_enabled(request.crm_user.organization):
        raise Http404("Hosted Account is not enabled for this organization.")

    account = get_object_or_404(
        WhatsAppAccount.objects.select_related("organization"),
        id=account_id,
        organization=request.crm_user.organization,
        connection_type="hosted",
        is_active=True,
    )

    items = []
    now = timezone.now()
    health_pause = automation_pause_until(account=account)
    sender_available_at = next_ai_send_at(account)
    session_settings = get_session_settings(account=account)
    account_connected = account.status == WhatsAppAccount.Status.CONNECTED
    followup_scheduler_enabled = bool(
        AutoFollowupSettings.objects.filter(
            organization=account.organization,
            enabled=True,
        ).exists()
    )

    # Durable Hosted AI jobs are the source of truth for pending AI work. Show
    # the exact inbound message that owns the job and its effective execution
    # time, including Account Health pauses. A blocked job stays visible with a
    # clear reason instead of pretending it will execute at a stale timestamp.
    jobs_query = (
        HostedAutomationJob.objects.filter(
            account=account,
            status__in=[
                HostedAutomationJob.Status.QUEUED,
                HostedAutomationJob.Status.PROCESSING,
            ],
        )
        .select_related("account", "lead", "lead__pipeline", "lead__stage", "source_message")
        .annotate(send_priority=Case(
            When(kind=HostedAutomationJob.Kind.WELCOME, then=Value(1)),
            default=Value(2), output_field=IntegerField(),
        ))
        .order_by("send_priority", "created_at", "id")
    )
    pending_ai_count = jobs_query.count()
    jobs = jobs_query[:200]
    for job in jobs:
        is_welcome = job.kind == HostedAutomationJob.Kind.WELCOME
        block_reason = job_ai_block_reason(job=job)
        execute_at = _latest_time(job.available_at, health_pause, sender_available_at)
        effective_status = job.status
        if block_reason:
            status_text = f"Blocked: {_reason_label(block_reason)}"
            effective_status = "blocked"
            execute_at = None
        elif not account_connected:
            status_text = "Waiting for Hosted Account connection"
            effective_status = "waiting_for_connection"
            execute_at = None
        elif job.status == HostedAutomationJob.Status.PROCESSING:
            stale_before = now - timedelta(seconds=HOSTED_AI_PROCESSING_STALE_SECONDS)
            lease_expired = (
                job.lease_expires_at <= now if job.lease_expires_at else
                job.started_at is None or job.started_at <= stale_before
            )
            if lease_expired:
                status_text = "Waiting for worker recovery"
                effective_status = "recovering"
            else:
                status_text = "Generating or sending message"
            execute_at = None
        elif health_pause and execute_at == health_pause:
            status_text = "Paused by Account Health"
            effective_status = "paused"
        elif sender_available_at and sender_available_at > now:
            status_text = f"Waiting for sender · {AI_SEND_GAP_SECONDS}-second minimum gap"
        elif job.available_at <= now:
            status_text = "Queued · waiting for worker"
        else:
            status_text = "Queued · scheduled"

        inbound_body = str(job.source_message.body or "").strip() if job.source_message else ""
        body = "Welcome message · content is prepared when this job runs" if is_welcome else "AI reply to lead message"
        if inbound_body and not is_welcome:
            body = f"AI reply to: {inbound_body[:180]}"
        label = "Welcome message" if is_welcome else "AI reply"

        items.append(
            {
                "id": str(job.id),
                "to": job.lead.phone,
                "lead": job.lead.name,
                "body": body,
                "message_type": label,
                "created_at": job.created_at.isoformat(),
                "available_at": _iso(execute_at),
                "origin": f"{label} · {status_text}",
                "priority": 1 if is_welcome else 2,
                "status": job.status,
                "effective_status": effective_status,
                "started_at": _iso(job.started_at),
                "lease_expires_at": _iso(job.lease_expires_at),
                "block_reason": block_reason,
                "next_step": "Generate and send welcome message" if is_welcome else "Generate and send AI reply",
            }
        )

    # LeadSequenceState is the scheduler's canonical next-step state. Scope it
    # directly to this exact Hosted account so the modal cannot show work from
    # another sender. Include paused rows as real queue state rather than
    # silently dropping them.
    states = (
        LeadSequenceState.objects.filter(
            organization=account.organization,
            sequence__whatsapp_account=account,
            sequence__is_active=True,
            status__in=[
                LeadSequenceState.Status.ACTIVE,
                LeadSequenceState.Status.PAUSED,
            ],
            lead_auto_followup_enabled=True,
            next_step__isnull=False,
        )
        .select_related("lead", "next_step", "sequence")
        .order_by("upcoming_send_at", "assigned_at")[:300]
    )
    for state in states:
        step = state.next_step
        execute_at = _latest_time(
            state.upcoming_send_at,
            state.paused_until,
            health_pause,
        )

        if not session_settings.get("auto_follow_up", True):
            status_text = "Paused: Auto Follow-up disabled"
            execute_at = None
        elif not followup_scheduler_enabled:
            status_text = "Paused: Follow-up scheduler disabled"
            execute_at = None
        elif state.status == LeadSequenceState.Status.PAUSED and not state.paused_until:
            status_text = "Paused"
            execute_at = None
        elif not account_connected:
            status_text = "Waiting for Hosted Account connection"
            execute_at = None
        elif health_pause and execute_at == health_pause:
            status_text = "Paused by Account Health"
        elif state.paused_until and execute_at == state.paused_until:
            status_text = "Waiting after lead activity"
        elif execute_at:
            status_text = "Scheduled"
        else:
            status_text = "Waiting for scheduler"

        body = step.title or step.get_step_type_display()
        if step.step_type == step.StepType.WHATSAPP:
            try:
                body = step.hosted_config.body
            except HostedFollowupStepConfig.DoesNotExist:
                pass

        items.append(
            {
                "id": str(state.id),
                "to": state.lead.phone,
                "lead": state.lead.name,
                "body": body,
                "message_type": step.get_step_type_display(),
                "created_at": state.assigned_at.isoformat(),
                "available_at": _iso(execute_at),
                "origin": (
                    f"Auto Follow-up · Step {step.position} · {status_text}"
                ),
                "priority": 3,
                "status": state.status,
                "next_step": step.title or step.get_step_type_display(),
            }
        )

    # Only show raw queued outbound rows that are not already represented by a
    # durable AI job or a sequence state. This removes duplicate/stale AI and
    # follow-up cards from the queue while still exposing a genuinely pending
    # manual Hosted message.
    pending_messages = _unrepresented_queued_messages(account, jobs_query)
    pending_ai_count += pending_messages.filter(
        Q(raw_payload__has_key="shvya_ai") | Q(raw_payload__has_key="shvya_welcome")
    ).count()
    queued_messages = (
        pending_messages.select_related("lead", "lead__pipeline", "lead__stage")
        .order_by("created_at")[:200]
    )
    shown_ai_count = len(jobs)
    for message in queued_messages:
        payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
        welcome = payload.get("shvya_welcome") or {}
        ai = payload.get("shvya_ai") or {}
        automatic = "shvya_ai" in payload or "shvya_welcome" in payload
        if automatic:
            shown_ai_count += 1

        hosted_payload = payload.get("shvya_hosted") or {}
        status_text = (
            "Pending delivery"
            if account_connected
            else "Waiting for Hosted Account connection"
        )
        execute_at = message.created_at if account_connected else None
        block_reason = ""
        if automatic:
            execute_at = _latest_time(execute_at, health_pause, sender_available_at) if account_connected else None
            block_reason = account_ai_block_reason(
                account=account, lead=message.lead,
                bump_up_number=ai.get("number", 1) if ai.get("origin") == "bump_up" else None,
            )
            if block_reason:
                status_text = f"Blocked: {_reason_label(block_reason)}"
                execute_at = None
        label = (
            "Welcome message" if welcome else
            "Bump-up message" if ai.get("origin") == "bump_up" else
            "AI reply" if automatic else hosted_payload.get("origin") or "Queued message"
        )
        items.append(
            {
                "id": str(message.id),
                "to": message.to_number,
                "lead": message.lead.name if message.lead else "",
                "body": message.body,
                "message_type": label if automatic else message.message_type,
                "created_at": message.created_at.isoformat(),
                "available_at": _iso(execute_at),
                "origin": f"{label} · {status_text}",
                "priority": 1 if welcome else 2 if automatic else 4,
                "status": message.status,
                "block_reason": block_reason,
                "next_step": "Send queued message",
            }
        )

    items.sort(key=_queue_sort_key)
    ai_head = next((item for item in items if item["priority"] <= 2), None)
    next_candidates = [
        item["available_at"] for item in items
        if item["priority"] > 2 and item.get("available_at")
    ]
    if ai_head and ai_head.get("available_at"):
        next_candidates.append(ai_head["available_at"])
    next_execution_at = min(
        next_candidates,
        default="",
    )

    return JsonResponse(
        {
            "ok": True,
            "phone_number": account.display_phone_number,
            "items": items,
            "pending_ai_count": pending_ai_count,
            "shown_ai_count": shown_ai_count,
            "ai_queue_has_more": pending_ai_count > shown_ai_count,
            "ai_min_send_gap_seconds": AI_SEND_GAP_SECONDS,
            "sender_available_at": _iso(sender_available_at),
            "next_execution_at": next_execution_at,
            "updated_at": now.isoformat(),
        }
    )
