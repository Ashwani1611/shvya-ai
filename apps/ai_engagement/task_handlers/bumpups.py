"""AI bump-up Celery boundary.

The public Celery task name is intentionally preserved.  Business orchestration
lives here instead of the historical apps.ai_engagement.tasks compatibility
module so that the canonical task module remains a thin registry/entrypoint.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from apps.ai_engagement.prompts import BUMP_UP_MESSAGE_INSTRUCTIONS

logger = logging.getLogger(__name__)


@shared_task(name="ai.dispatch_bump_ups")
def dispatch_bump_ups():
    """Queue at most one AI-written bump per lead after each silent hour."""
    from apps.ai_engagement.services.ai_permissions import AIPermissionService
    from apps.ai_engagement.services.engagement import EngagementService
    from apps.ai_engagement.services.org_info import OrgInfoService
    from apps.channels.models import WhatsAppMessage
    from apps.channels.tasks import send_whatsapp_message_task
    from apps.crm.models import Lead
    from services.channels.hosted_whatsapp_service import (
        account_ai_block_reason,
        get_session_settings,
    )
    from services.channels.whatsapp_service import (
        queue_outbound_message,
        resolve_account_for_lead,
    )

    now = timezone.now()
    queued = 0
    leads = Lead.objects.select_related("organization", "pipeline", "stage").filter(
        organization__is_active=True,
        pipeline__is_active=True,
        ai_enabled=True,
    ).iterator(chunk_size=200)

    for lead in leads:
        if not AIPermissionService().evaluate(
            organization=lead.organization,
            lead=lead,
        ).allowed:
            continue

        org_info = OrgInfoService().get_or_create(organization=lead.organization)
        if not org_info.bump_up_enabled or org_info.bump_up_count < 1:
            continue

        account = resolve_account_for_lead(
            organization=lead.organization,
            lead=lead,
        )
        if account is None:
            continue

        account_settings = get_session_settings(account=account)
        if not account_settings.get("ai_auto_reply") or not account_settings.get(
            "bump_up_messages"
        ):
            continue

        recent = list(lead.whatsapp_messages.order_by("-created_at", "-id")[:100])
        if not recent or recent[0].direction != WhatsAppMessage.Direction.OUTBOUND:
            continue
        if recent[0].created_at > now - timedelta(hours=1):
            continue

        latest_inbound = next(
            (
                message
                for message in recent
                if message.direction == WhatsAppMessage.Direction.INBOUND
            ),
            None,
        )
        if (
            latest_inbound is None
            or latest_inbound.created_at <= now - timedelta(hours=24)
        ):
            continue

        bump_messages = [
            message
            for message in recent
            if isinstance(message.raw_payload, dict)
            and message.raw_payload.get("shvya_ai", {}).get("origin") == "bump_up"
            and message.created_at > latest_inbound.created_at
        ]
        limit = min(
            int(org_info.bump_up_count),
            int(account_settings.get("bump_up_count", 1)),
        )
        if len(bump_messages) >= limit:
            continue

        service = EngagementService()
        try:
            context = service.context_builder.build(
                organization=lead.organization,
                lead=lead,
                knowledge_query=recent[0].body,
            )
            instructions = (
                service._build_instructions(context=context)
                + "\n\n"
                + BUMP_UP_MESSAGE_INSTRUCTIONS
            )
            provider = service.provider or __import__(
                "apps.ai_engagement.services.ai_provider",
                fromlist=["OpenAIProvider"],
            ).OpenAIProvider()
            result = provider.generate_text(
                instructions=instructions,
                input_text=service._build_input(context=context),
                metadata={
                    "organization_id": str(lead.organization_id),
                    "lead_id": str(lead.id),
                    "task": "bump_up",
                },
            )
            decision = service._normalize_result(result=result)
            service._validate_engagement_policy(
                decision=decision,
                context=context,
            )
        except Exception:
            logger.exception("Unable to generate bump-up for lead %s", lead.id)
            continue

        if not decision.should_engage or not decision.message.strip():
            continue

        with transaction.atomic():
            locked_lead = (
                Lead.objects.select_for_update()
                .select_related("organization", "pipeline", "stage")
                .get(pk=lead.pk)
            )
            latest = locked_lead.whatsapp_messages.order_by(
                "-created_at",
                "-id",
            ).first()
            if latest is None or latest.id != recent[0].id:
                continue

            if account_ai_block_reason(
                account=account,
                lead=locked_lead,
                bump_up_number=len(bump_messages) + 1,
            ):
                continue

            outbound = queue_outbound_message(
                organization=locked_lead.organization,
                account=account,
                to_number=locked_lead.phone,
                body=decision.message.strip(),
                lead=locked_lead,
            )
            outbound.raw_payload = {
                "shvya_ai": {
                    "origin": "bump_up",
                    "number": len(bump_messages) + 1,
                    "model": decision.model,
                }
            }
            outbound.save(update_fields=["raw_payload", "updated_at"])
            transaction.on_commit(
                lambda message_id=str(outbound.id): send_whatsapp_message_task.delay(
                    message_id
                )
            )
            queued += 1

    return {"queued": queued}
