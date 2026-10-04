"""Instagram transport for the existing follow-up sequence engine."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.channels.instagram_models import InstagramConversation, InstagramMessage
from apps.followups.models import FollowupExecution, FollowupStep, LeadSequenceState
from services.followup_service import (
    FollowupError,
    FollowupDeliveryUnconfirmed,
    _next_position,
    _recalculate_active_states,
    _render_text,
    _repeat_or_advance,
    _validate_schedule,
)


def conversation_for_sequence(lead, sequence):
    if sequence.organization_id != lead.organization_id:
        raise FollowupError("Lead and sequence belong to different organizations.")
    conversations = list(
        InstagramConversation.objects.filter(
            organization_id=lead.organization_id,
            lead=lead,
            account_id=sequence.instagram_account_id,
            account__organization_id=lead.organization_id,
            account__status="connected",
        ).select_related("account")[:2]
    )
    if len(conversations) != 1:
        raise FollowupError(
            "The lead must have exactly one conversation on this connected Instagram account."
        )
    return conversations[0]


def validate_body(body):
    body = str(body or "").strip()
    if not body or len(body) > 1000:
        raise FollowupError("Instagram message must contain 1–1000 characters.")
    return body


@transaction.atomic
def add_step(*, sequence, body, title="", **schedule):
    if not sequence.instagram_account_id:
        raise FollowupError("Instagram messages require an Instagram sequence.")
    body = validate_body(body)
    _validate_schedule(**schedule)
    step = FollowupStep.objects.create(
        sequence=sequence,
        position=_next_position(sequence),
        step_type=FollowupStep.StepType.INSTAGRAM,
        instagram_body=body,
        title=title.strip() or "Instagram DM",
        **schedule,
    )
    _recalculate_active_states(sequence)
    return step


def send_step(state, step, execution):
    from apps.channels.instagram_tasks import send_instagram_message_task
    from services.channels.instagram_inbox import queue_inbox_reply

    conversation = conversation_for_sequence(state.lead, state.sequence)
    message_id = (execution.payload or {}).get("instagram_message_id")
    message = (
        InstagramMessage.objects.filter(
            pk=message_id,
            organization=state.organization,
            conversation=conversation,
        ).first()
        if message_id
        else None
    )
    if message_id and message is None:
        raise FollowupDeliveryUnconfirmed(
            "Instagram delivery record is missing; review before restarting."
        )
    if message is None:
        message = queue_inbox_reply(
            state.organization,
            conversation_id=conversation.pk,
            body=validate_body(
                _render_text(
                    step.instagram_body, state.lead, user=state.sequence.created_by
                )
            ),
            idempotency_key=execution.pk,
        )
        message.raw_payload = {
            **message.raw_payload,
            "shvya_followup": {"execution_id": str(execution.pk)},
        }
        message.save(update_fields=["raw_payload", "updated_at"])
        execution.payload = {"instagram_message_id": str(message.pk)}
        execution.save(update_fields=["payload", "updated_at"])
    if message.status in {InstagramMessage.Status.SENT, InstagramMessage.Status.READ}:
        now = message.sent_at or timezone.now()
        execution.status = FollowupExecution.Status.SENT
        execution.finished_at = now
        execution.save(update_fields=["status", "finished_at", "updated_at"])
        state.last_sent_at = now
        state.save(update_fields=["last_sent_at", "updated_at"])
        _repeat_or_advance(state, step, completed_at=now)
    elif message.status == InstagramMessage.Status.FAILED:
        raise FollowupDeliveryUnconfirmed(
            message.error or "Instagram delivery failed. Review before restarting."
        )
    elif message.raw_payload.get("shvya_send_claimed_at"):
        if message.updated_at < timezone.now() - timedelta(minutes=5):
            raise FollowupDeliveryUnconfirmed(
                "Instagram delivery outcome is unknown. Review before restarting."
            )
    else:
        transaction.on_commit(
            lambda: send_instagram_message_task.delay(str(message.pk)), robust=True
        )


def dispatch_due():
    from services.followup_service import process_due_state
    from django.core.cache import cache

    key = "shvya:instagram:followup-dispatch"
    if not cache.add(key, "1", timeout=55):
        return {"status": "locked"}
    try:
        state_ids = list(
            LeadSequenceState.objects.filter(
                sequence__instagram_account__isnull=False,
                sequence__is_active=True,
                status=LeadSequenceState.Status.ACTIVE,
                lead_auto_followup_enabled=True,
                lead__auto_followup_enabled=True,
                upcoming_send_at__lte=timezone.now(),
            )
            .order_by("upcoming_send_at")
            .values_list("pk", flat=True)[:20]
        )
        for state_id in state_ids:
            process_due_state(state_id)
        return {"processed": len(state_ids)}
    finally:
        cache.delete(key)


def reschedule(*, organization_id):
    from apps.channels.services.instagram_automation import get_settings
    from services.followup_service import (
        calculate_step_due,
        live_followup_due,
        _move_into_business_hours,
    )

    controls = get_settings(organization_id=organization_id)
    ids = LeadSequenceState.objects.filter(
        organization_id=organization_id,
        sequence__instagram_account__isnull=False,
        status=LeadSequenceState.Status.ACTIVE,
        next_step__isnull=False,
    ).values_list("pk", flat=True)
    for state_id in ids.iterator():
        with transaction.atomic():
            state = (
                LeadSequenceState.objects.select_for_update(of=("self",))
                .select_related(
                    "organization",
                    "lead",
                    "sequence",
                    "next_step",
                )
                .get(pk=state_id, organization_id=organization_id)
            )
            if (
                not state.next_step_id
                or state.status != LeadSequenceState.Status.ACTIVE
            ):
                continue
            reference = (
                state.last_step_completed_at or state.activated_at or state.assigned_at
            )
            scheduled = calculate_step_due(
                step=state.next_step,
                reference=reference,
                organization=state.organization,
            )
            eligible = live_followup_due(state, automation_settings=controls)
            state.upcoming_send_at = _move_into_business_hours(
                organization=state.organization,
                due=max(scheduled, eligible),
                automation_settings=controls,
            )
            state.save(update_fields=["upcoming_send_at", "updated_at"])
