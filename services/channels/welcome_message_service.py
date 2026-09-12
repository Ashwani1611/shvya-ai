"""Automatic welcome-message orchestration for newly created CRM leads.

A welcome is eligible only when the lead is created directly in the protected
New Lead/New Leads stage and the pipeline's configured WhatsApp number exactly
matches one active connected account. There is deliberately no organization
fallback: a welcome must never leave from an unrelated number.
"""

import logging

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.ai_provider import OpenAIProvider
from apps.channels.models import WhatsAppAccount, WhatsAppMessage, WhatsAppTemplate
from apps.crm.models import Lead
from services.channels.hosted_whatsapp_service import (
    normalize_whatsapp_number,
    pipeline_whatsapp_number,
)
from services.channels.whatsapp_service import queue_outbound_message
from services.channels.whatsapp_template_delivery import (
    WhatsAppTemplateSendError,
    queue_template_message,
)

logger = logging.getLogger(__name__)

NEW_LEAD_STAGE_NAMES = frozenset({"new lead", "new leads"})
WELCOME_TRIGGER = "lead_created"


def _is_new_lead_stage(lead):
    stage_name = str(getattr(lead.stage, "name", "") or "").strip().casefold()
    return stage_name in NEW_LEAD_STAGE_NAMES


def _linked_account_for_lead(lead):
    """Resolve only the account whose number exactly matches the lead pipeline."""
    expected_number = pipeline_whatsapp_number(lead.pipeline)
    if not expected_number:
        return None

    accounts = (
        WhatsAppAccount.objects.filter(
            organization=lead.organization,
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        .order_by("-updated_at")
    )

    matches = []
    for account in accounts:
        account_number = normalize_whatsapp_number(
            phone_number=account.display_phone_number or account.phone_number_id
        )
        if account_number == expected_number:
            matches.append(account)

    if len(matches) > 1:
        logger.warning(
            "Multiple active WhatsApp accounts match pipeline %s number %s; "
            "using the most recently updated account %s.",
            lead.pipeline_id,
            expected_number,
            matches[0].id,
        )
    return matches[0] if matches else None


def _already_has_welcome(*, lead, account):
    return WhatsAppMessage.objects.filter(
        organization=lead.organization,
        lead=lead,
        account=account,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        raw_payload__shvya_welcome__trigger=WELCOME_TRIGGER,
    ).exists()


def _mark_welcome_message(*, message, source):
    payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    message.raw_payload = {
        **payload,
        "shvya_welcome": {
            "trigger": WELCOME_TRIGGER,
            "source": source,
        },
    }
    message.save(update_fields=["raw_payload", "updated_at"])
    return message


def _generate_hosted_welcome(*, lead):
    """Generate one concise welcome grounded only in Organization Information."""
    org_info = OrgInfo.objects.filter(organization=lead.organization).first()
    about = str(getattr(org_info, "about", "") or "").strip()
    languages = str(getattr(org_info, "bot_languages", "") or "").strip()
    engagement = str(getattr(org_info, "engagement_instructions", "") or "").strip()
    first_name = (lead.name or "").strip().split(" ")[0]

    provider = OpenAIProvider()
    result = provider.generate_text(
        instructions=(
            "Write the first outbound WhatsApp welcome for a newly created sales lead. "
            "Use only the supplied Organization Information; do not invent facts, "
            "prices, offers, locations, promises, or availability. Keep it natural, "
            "professional, and brief (maximum 45 words). This message is a welcome only: "
            "do not ask qualification questions or claim any action was completed. "
            "Follow the supplied language/tone instructions when present. Return only "
            "the customer-facing message, with no JSON, labels, or markdown headings."
        ),
        input_text=(
            f"Organization name: {lead.organization.name}\n"
            f"Lead first name: {first_name}\n"
            f"About organization: {about}\n"
            f"Configured languages: {languages}\n"
            f"Engagement instructions: {engagement}"
        ),
        metadata={
            "feature": "engagement",
            "organization_id": str(lead.organization_id),
            "lead_id": str(lead.id),
            "trigger": "new_lead_welcome",
        },
    )
    return str(result.text or "").strip().strip('"')


def _queue_api_welcome(*, lead, account):
    template_name = str(account.welcome_message or "").strip()
    if not template_name:
        return {"status": "skipped", "reason": "welcome_template_not_configured"}

    template = (
        WhatsAppTemplate.objects.filter(
            organization=lead.organization,
            account=account,
            name=template_name,
            status=WhatsAppTemplate.Status.APPROVED,
            attachment_type=WhatsAppTemplate.AttachmentType.NONE,
        )
        .exclude(meta_template_id="")
        .first()
    )
    if not template:
        return {"status": "skipped", "reason": "welcome_template_not_approved"}

    try:
        message = queue_template_message(template=template, lead=lead)
    except WhatsAppTemplateSendError as exc:
        logger.warning(
            "Could not queue API welcome template %s for lead %s: %s",
            template.id,
            lead.id,
            exc,
        )
        return {"status": "skipped", "reason": "welcome_template_invalid"}

    _mark_welcome_message(message=message, source="whatsapp_api_template")

    from apps.channels.tasks import send_whatsapp_message_task

    send_whatsapp_message_task.delay(str(message.id))
    return {"status": "queued", "message_id": str(message.id), "transport": "api"}


def _queue_hosted_welcome(*, lead, account):
    body = _generate_hosted_welcome(lead=lead)
    if not body:
        return {"status": "skipped", "reason": "empty_ai_welcome"}

    message = queue_outbound_message(
        organization=lead.organization,
        account=account,
        to_number=lead.phone,
        body=body,
        lead=lead,
    )
    _mark_welcome_message(message=message, source="organization_information_ai")

    from apps.channels.hosted_send_tasks import send_hosted_whatsapp_message_task

    send_hosted_whatsapp_message_task.delay(str(message.id))
    return {"status": "queued", "message_id": str(message.id), "transport": "hosted"}


def send_new_lead_welcome(*, lead_id):
    """Queue the correct welcome transport for one newly-created lead."""
    lead = (
        Lead.objects.select_related("organization", "pipeline", "stage")
        .filter(id=lead_id)
        .first()
    )
    if not lead:
        return {"status": "skipped", "reason": "lead_not_found"}
    if not _is_new_lead_stage(lead):
        return {"status": "skipped", "reason": "not_new_leads_stage"}

    account = _linked_account_for_lead(lead)
    if not account:
        return {"status": "skipped", "reason": "pipeline_number_not_connected"}
    if _already_has_welcome(lead=lead, account=account):
        return {"status": "skipped", "reason": "welcome_already_queued"}

    if account.connection_type == WhatsAppAccount.ConnectionType.API:
        return _queue_api_welcome(lead=lead, account=account)
    if account.connection_type == WhatsAppAccount.ConnectionType.coexisted:
        return _queue_hosted_welcome(lead=lead, account=account)

    return {"status": "skipped", "reason": "unsupported_connection_type"}
