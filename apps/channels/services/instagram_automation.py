"""Instagram account controls, independent of WhatsApp pipeline settings."""

from copy import deepcopy
from types import SimpleNamespace

from django.db import transaction

from apps.organizations.models import Organization
from services.channels.hosted_whatsapp_service import (
    HostedWhatsAppValidationError,
    _normalize_session_settings_payload,
)

DEFAULTS = {
    "ai_auto_reply": True,
    "auto_lead_creation": True,
    "bump_up_messages": False,
    "bump_up_count": 2,
    "auto_follow_up": False,
    "business_hours_start": "07:30",
    "business_hours_end": "19:30",
    "active_conversation_delay_value": 2,
    "active_conversation_delay_unit": "hours",
}


def get_settings(*, organization_id):
    settings = (
        Organization.objects.filter(pk=organization_id)
        .values_list("settings", flat=True)
        .first()
        or {}
    )
    return {**DEFAULTS, **deepcopy(settings.get("instagram_automation", {}))}


@transaction.atomic
def update_settings(*, organization_id, payload):
    if not isinstance(payload, dict):
        raise HostedWhatsAppValidationError("Settings must be a JSON object.")
    organization = Organization.objects.select_for_update().get(pk=organization_id)
    root = deepcopy(organization.settings or {})
    current = {**DEFAULTS, **root.get("instagram_automation", {})}
    updated = _normalize_session_settings_payload(
        current=current,
        payload=payload,
        pipeline=SimpleNamespace(ai_enabled=current["ai_auto_reply"]),
    )
    root["instagram_automation"] = updated
    organization.settings = root
    organization.save(update_fields=["settings", "updated_at"])
    if any(
        current[key] != updated[key]
        for key in (
            "auto_follow_up",
            "business_hours_start",
            "business_hours_end",
            "active_conversation_delay_value",
            "active_conversation_delay_unit",
        )
    ):
        from apps.followups.instagram import reschedule

        transaction.on_commit(
            lambda: reschedule(organization_id=organization_id), robust=True
        )
    return updated


def outbound_allowed(message):
    """Recheck persisted switches at delivery time, including queued work."""
    from django.utils import timezone
    from apps.ai_engagement.services.ai_permissions import AIPermissionService
    from apps.followups.models import FollowupExecution
    from services.followup_service import live_followup_due

    metadata = message.raw_payload or {}
    controls = get_settings(organization_id=message.organization_id)
    if metadata.get("shvya_followup"):
        execution = (
            FollowupExecution.objects.filter(
                pk=metadata["shvya_followup"].get("execution_id"),
                organization_id=message.organization_id,
                state__sequence__instagram_account_id=message.account_id,
                lead_id=message.conversation.lead_id,
                payload__instagram_message_id=str(message.pk),
                status=FollowupExecution.Status.PROCESSING,
            )
            .select_related("state__lead", "state__sequence", "state__organization")
            .first()
        )
        if (
            not execution
            or execution.state.next_step_id != execution.step_id
            or execution.created_at < execution.state.activated_at
        ):
            return False
        now = timezone.now()
        return (
            live_followup_due(execution.state, automation_settings=controls, now=now)
            <= now
        )
    ai = metadata.get("shvya_ai")
    if ai:
        lead = message.conversation.lead
        if (
            not lead
            or not AIPermissionService()
            .evaluate(organization=lead.organization, lead=lead, channel="instagram")
            .allowed
        ):
            return False
        if ai.get("origin") == "bump_up":
            from apps.ai_engagement.services.org_info import OrgInfoService

            org_info = OrgInfoService().get_or_create(organization=lead.organization)
            if not controls["bump_up_messages"] or not org_info.bump_up_enabled:
                return False
            if int(ai.get("number", 1)) > min(
                controls["bump_up_count"], org_info.bump_up_count
            ):
                return False
            from apps.channels.instagram_models import InstagramMessage

            latest_inbound = (
                message.conversation.messages.filter(
                    direction=InstagramMessage.Direction.INBOUND
                )
                .order_by("-created_at", "-id")
                .first()
            )
            if not latest_inbound or str(latest_inbound.pk) != ai.get(
                "source_inbound_message_id"
            ):
                return False
    return True


def dispatch_bumps():
    """Use the canonical AI prompt and exact Instagram history for silent turns."""
    import logging
    from datetime import timedelta
    from django.utils import timezone
    from apps.ai_engagement.prompts import BUMP_UP_MESSAGE_INSTRUCTIONS
    from apps.ai_engagement.services.ai_permissions import AIPermissionService
    from apps.ai_engagement.services.ai_provider import OpenAIProvider
    from apps.ai_engagement.services.engagement import EngagementService
    from apps.ai_engagement.services.org_info import OrgInfoService
    from apps.channels.instagram_models import InstagramConversation
    from apps.channels.instagram_tasks import send_instagram_message_task
    from services.channels.instagram_ai import InstagramAIContextBuilder
    from services.channels.instagram_inbox import conversation_policy, queue_inbox_reply

    # Recover broker publication failures without repeating claimed sends.
    from apps.channels.instagram_models import InstagramMessage

    pending = (
        InstagramMessage.objects.filter(
            status="queued",
            raw_payload__shvya_ai__origin="bump_up",
            account__status="connected",
        )
        .exclude(raw_payload__has_key="shvya_send_claimed_at")
        .select_related("conversation__lead", "account")[:100]
    )
    for message in pending:
        if outbound_allowed(message):
            send_instagram_message_task.delay(str(message.pk))

    queued = 0
    now = timezone.now()
    conversations = InstagramConversation.objects.filter(
        account__status="connected",
        organization__is_active=True,
        lead__isnull=False,
        lead__ai_enabled=True,
        lead__pipeline__is_active=True,
        last_message_at__gte=now - timedelta(hours=24),
    ).select_related("account", "organization", "lead__pipeline", "lead__stage")
    for conversation in conversations.iterator(chunk_size=100):
        controls = get_settings(organization_id=conversation.organization_id)
        if not controls["bump_up_messages"] or not controls["ai_auto_reply"]:
            continue
        lead = conversation.lead
        if (
            not AIPermissionService()
            .evaluate(
                organization=conversation.organization, lead=lead, channel="instagram"
            )
            .allowed
        ):
            continue
        org_info = OrgInfoService().get_or_create(
            organization=conversation.organization
        )
        if not org_info.bump_up_enabled:
            continue
        recent = list(conversation.messages.order_by("-created_at", "-id")[:100])
        if (
            not recent
            or recent[0].direction != "outbound"
            or recent[0].status not in {"sent", "read"}
            or recent[0].created_at > now - timedelta(hours=1)
        ):
            continue
        inbound = next((row for row in recent if row.direction == "inbound"), None)
        if not inbound or not conversation_policy(conversation)["can_reply"]:
            continue
        count = sum(
            1
            for row in recent
            if row.created_at > inbound.created_at
            and (row.raw_payload or {}).get("shvya_ai", {}).get("origin") == "bump_up"
        )
        if count >= min(controls["bump_up_count"], org_info.bump_up_count):
            continue
        try:
            service = EngagementService(
                context_builder=InstagramAIContextBuilder(
                    conversation_id=conversation.pk
                )
            )
            context = service.context_builder.build(
                organization=conversation.organization,
                lead=lead,
                knowledge_query=recent[0].body,
            )
            result = (service.provider or OpenAIProvider()).generate_text(
                instructions=service._build_instructions(context=context)
                + "\n\n"
                + BUMP_UP_MESSAGE_INSTRUCTIONS,
                input_text=service._build_input(context=context),
                metadata={
                    "organization_id": str(lead.organization_id),
                    "lead_id": str(lead.pk),
                    "task": "bump_up",
                },
            )
            decision = service._normalize_result(result=result)
            service._validate_engagement_policy(decision=decision, context=context)
            if not decision.should_engage or not decision.message.strip():
                continue
            with transaction.atomic():
                # Match queue_inbox_reply's account lock before conversation locks.
                from apps.channels.instagram_models import InstagramAccount

                InstagramAccount.objects.select_for_update().get(
                    pk=conversation.account_id,
                    organization_id=conversation.organization_id,
                )
                locked = InstagramConversation.objects.select_for_update().get(
                    pk=conversation.pk, organization_id=conversation.organization_id
                )
                latest = locked.messages.order_by("-created_at", "-id").first()
                if (
                    locked.lead_id != lead.pk
                    or latest is None
                    or latest.pk != recent[0].pk
                ):
                    continue
                message = queue_inbox_reply(
                    conversation.organization,
                    conversation_id=locked.pk,
                    body=decision.message.strip(),
                )
                message.raw_payload = {
                    "shvya_ai": {
                        "origin": "bump_up",
                        "number": count + 1,
                        "model": decision.model,
                        "source_inbound_message_id": str(inbound.pk),
                    }
                }
                message.save(update_fields=["raw_payload", "updated_at"])
                if not outbound_allowed(message):
                    message.delete()
                    continue
                transaction.on_commit(
                    lambda message_id=str(message.pk): (
                        send_instagram_message_task.delay(message_id)
                    ),
                    robust=True,
                )
                queued += 1
        except Exception:
            logging.getLogger(__name__).exception(
                "Instagram bump-up failed for conversation %s", conversation.pk
            )
    return {"queued": queued}
