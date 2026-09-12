from datetime import datetime, timezone as datetime_timezone

from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.decorators import crm_login_required
from apps.followups.models import AutoFollowupSettings, LeadSequenceState
from apps.hosted_automation.models import HostedAutomationJob, HostedFollowupStepConfig
from apps.organizations.features import is_hosted_account_enabled
from services.channels.hosted_automation_service import (
    automation_pause_until,
    hosted_ai_block_reason,
)
from services.channels.hosted_whatsapp_service import get_session_settings


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
    return when, int(item.get("priority") or 9), item.get("created_at") or ""


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
    health_pause = automation_pause_until(account=account)
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
    jobs = (
        HostedAutomationJob.objects.filter(
            account=account,
            status__in=[
                HostedAutomationJob.Status.QUEUED,
                HostedAutomationJob.Status.PROCESSING,
            ],
        )
        .select_related("lead", "source_message")
        .order_by("available_at", "created_at")[:200]
    )
    for job in jobs:
        block_reason = hosted_ai_block_reason(account=account, lead=job.lead)
        execute_at = _latest_time(job.available_at, health_pause)
        if block_reason:
            status_text = f"Blocked: {_reason_label(block_reason)}"
            execute_at = None
        elif not account_connected:
            status_text = "Waiting for Hosted Account connection"
            execute_at = None
        elif job.status == HostedAutomationJob.Status.PROCESSING:
            status_text = "Processing now"
            execute_at = None
        elif health_pause and execute_at == health_pause:
            status_text = "Paused by Account Health"
        else:
            status_text = "Scheduled"

        inbound_body = str(job.source_message.body or "").strip()
        body = "AI reply to latest lead message"
        if inbound_body:
            body = f"AI reply to: {inbound_body[:180]}"

        items.append(
            {
                "id": str(job.id),
                "to": job.lead.phone,
                "lead": job.lead.name,
                "body": body,
                "message_type": "AI Engagement",
                "created_at": job.created_at.isoformat(),
                "available_at": _iso(execute_at),
                "origin": f"AI Engagement · {status_text}",
                "priority": 1,
                "status": job.status,
                "next_step": "Generate and send AI reply",
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
                "priority": 2,
                "status": state.status,
                "next_step": step.title or step.get_step_type_display(),
            }
        )

    # Only show raw queued outbound rows that are not already represented by a
    # durable AI job or a sequence state. This removes duplicate/stale AI and
    # follow-up cards from the queue while still exposing a genuinely pending
    # manual Hosted message.
    queued_messages = (
        WhatsAppMessage.objects.filter(
            organization=account.organization,
            account=account,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            status=WhatsAppMessage.Status.QUEUED,
        )
        .select_related("lead")
        .order_by("created_at")[:200]
    )
    for message in queued_messages:
        payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
        if payload.get("shvya_ai") or payload.get("shvya_auto_followup"):
            continue

        hosted_payload = payload.get("shvya_hosted") or {}
        status_text = (
            "Pending delivery"
            if account_connected
            else "Waiting for Hosted Account connection"
        )
        items.append(
            {
                "id": str(message.id),
                "to": message.to_number,
                "lead": message.lead.name if message.lead else "",
                "body": message.body,
                "message_type": message.message_type,
                "created_at": message.created_at.isoformat(),
                "available_at": (
                    message.created_at.isoformat() if account_connected else ""
                ),
                "origin": (
                    f"{hosted_payload.get('origin') or 'Queued message'} · {status_text}"
                ),
                "priority": 3,
                "status": message.status,
                "next_step": "Send queued message",
            }
        )

    # Upcoming means chronological. Priority only breaks ties at the same due
    # time; it must not place a later AI job ahead of an earlier follow-up.
    items.sort(key=_queue_sort_key)
    next_execution_at = next(
        (item["available_at"] for item in items if item.get("available_at")),
        "",
    )

    return JsonResponse(
        {
            "ok": True,
            "phone_number": account.display_phone_number,
            "items": items,
            "next_execution_at": next_execution_at,
            "updated_at": datetime.now(datetime_timezone.utc).isoformat(),
        }
    )
