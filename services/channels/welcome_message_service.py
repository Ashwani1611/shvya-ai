"""Automatic welcome-message orchestration for newly created CRM leads.

A welcome is eligible only when the lead is created directly in the protected
New Lead/New Leads stage and the pipeline's configured WhatsApp number exactly
matches one active connected account. There is deliberately no organization
fallback: a welcome must never leave from an unrelated number.
"""

import logging

from django.db import transaction
from django.utils import timezone

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.ai_provider import OpenAIProvider
from apps.channels.models import WhatsAppAccount, WhatsAppMessage, WhatsAppTemplate
from apps.crm.models import Lead
from services.channels.hosted_whatsapp_service import (
    account_ai_block_reason,
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


def _mark_welcome_message(*, message, source, job):
    payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    welcome_payload = {
        "trigger": WELCOME_TRIGGER,
        "source": source,
        "job_id": str(job.id),
        "scheduled_for": job.available_at.isoformat(),
    }

    message.raw_payload = {
        **payload,
        "shvya_welcome": welcome_payload,
    }
    message.save(update_fields=["raw_payload", "updated_at"])
    return message


def _generate_hosted_welcome(*, lead):
    """Generate one concise welcome grounded only in Organization Information."""
    org_info = OrgInfo.objects.filter(organization=lead.organization).first()
    about = str(getattr(org_info, "about", "") or "").strip()
    languages = str(getattr(org_info, "bot_languages", "") or "").strip()
    from apps.ai_engagement.services.playbook import parse_playbook, playbook_for_engagement
    raw_playbook = str(getattr(org_info, "ai_playbook", "") or "")
    configured_welcome = parse_playbook(raw_playbook)["welcome_message"]
    if configured_welcome:
        return configured_welcome
    engagement = playbook_for_engagement(raw_playbook)
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
            f"AI Playbook: {engagement}"
        ),
        metadata={
            "feature": "engagement",
            "organization_id": str(lead.organization_id),
            "lead_id": str(lead.id),
            "trigger": "new_lead_welcome",
        },
    )
    return str(result.text or "").strip().strip('"')


def _approved_welcome_template(*, lead, account):
    template_name = str(account.welcome_message or "").strip()
    if not template_name:
        return None, "welcome_template_not_configured"
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
    return template, "" if template else "welcome_template_not_approved"


def _welcome_block_reason(*, lead, account):
    if not _is_new_lead_stage(lead):
        return "not_new_leads_stage"
    linked = _linked_account_for_lead(lead)
    if linked is None or linked.pk != account.pk:
        return "pipeline_number_not_connected"
    return account_ai_block_reason(account=account, lead=lead)


def send_new_lead_welcome(*, lead_id):
    """Persist a welcome intent; generate and show it only when its turn is due."""
    from services.channels.hosted_automation_service import enqueue_hosted_welcome

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
    reason = _welcome_block_reason(lead=lead, account=account)
    if reason:
        return {"status": "skipped", "reason": reason}
    if _already_has_welcome(lead=lead, account=account):
        return {"status": "skipped", "reason": "welcome_already_queued"}
    if account.connection_type == WhatsAppAccount.ConnectionType.API:
        _, reason = _approved_welcome_template(lead=lead, account=account)
        if reason:
            return {"status": "skipped", "reason": reason}
    elif account.connection_type != WhatsAppAccount.ConnectionType.coexisted:
        return {"status": "skipped", "reason": "unsupported_connection_type"}

    job = enqueue_hosted_welcome(account=account, lead=lead)
    if job is None:
        return {"status": "skipped", "reason": "welcome_not_eligible"}
    result = {
        "status": job.status,
        "job_id": str(job.id),
        "transport": account.connection_type,
        "scheduled_for": job.available_at.isoformat(),
    }
    if (job.result or {}).get("reason"):
        result["reason"] = job.result["reason"]
    return result


def execute_queued_welcome(*, job):
    """Materialize one due welcome, with live controls checked before and after AI.

    The durable job worker persists this result before sending synchronously.
    Retrying the same job reuses its exact message instead of generating another
    welcome. Queued intents have no WhatsAppMessage row and cannot appear as
    customer-facing chat history before processing reaches them.
    """
    from apps.hosted_automation.models import HostedAutomationJob

    if (job.kind != HostedAutomationJob.Kind.WELCOME
            or job.status != HostedAutomationJob.Status.PROCESSING
            or job.available_at > timezone.now()):
        return {"status": "skipped", "reason": "welcome_not_due"}
    lead = (
        Lead.objects.select_related("organization", "pipeline", "stage")
        .filter(pk=job.lead_id, organization_id=job.organization_id)
        .first()
    )
    account = WhatsAppAccount.objects.filter(
        pk=job.account_id, organization_id=job.organization_id,
    ).first()
    if lead is None or account is None:
        return {"status": "skipped", "reason": "welcome_context_missing"}
    reason = _welcome_block_reason(lead=lead, account=account)
    if reason:
        return {"status": "skipped", "reason": reason}

    existing = WhatsAppMessage.objects.filter(
        organization_id=job.organization_id,
        lead=lead,
        account=account,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        raw_payload__shvya_welcome__trigger=WELCOME_TRIGGER,
    ).first()
    if existing is not None:
        return {"status": "completed", "engaged": True, "message_id": str(existing.id)}

    body = ""
    if account.connection_type == WhatsAppAccount.ConnectionType.coexisted:
        body = _generate_hosted_welcome(lead=lead)
        if not body:
            return {"status": "failed", "reason": "empty_ai_welcome"}

    with transaction.atomic():
        # Keep the welcome marker and chat row in one transaction; no observer
        # should see an unmarked welcome or a duplicate created by a redelivery.
        lead = Lead.objects.select_for_update().select_related(
            "organization", "pipeline", "stage",
        ).get(pk=lead.pk, organization_id=job.organization_id)
        account = WhatsAppAccount.objects.get(
            pk=account.pk, organization_id=job.organization_id,
        )
        reason = _welcome_block_reason(lead=lead, account=account)
        if reason:
            return {"status": "skipped", "reason": reason}
        existing = WhatsAppMessage.objects.filter(
            organization_id=job.organization_id,
            lead=lead,
            account=account,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            raw_payload__shvya_welcome__trigger=WELCOME_TRIGGER,
        ).first()
        if existing is not None:
            return {"status": "completed", "engaged": True, "message_id": str(existing.id)}
        if account.connection_type == WhatsAppAccount.ConnectionType.API:
            template, reason = _approved_welcome_template(lead=lead, account=account)
            if reason:
                return {"status": "skipped", "reason": reason}
            try:
                message = queue_template_message(template=template, lead=lead)
            except WhatsAppTemplateSendError as exc:
                logger.warning("Welcome template invalid for lead %s: %s", lead.id, exc)
                return {"status": "failed", "reason": "welcome_template_invalid"}
            source = "whatsapp_api_template"
        elif account.connection_type == WhatsAppAccount.ConnectionType.coexisted:
            message = queue_outbound_message(
                organization=lead.organization,
                account=account,
                to_number=lead.phone,
                body=body,
                lead=lead,
            )
            source = "organization_information_ai"
        else:
            return {"status": "skipped", "reason": "unsupported_connection_type"}
        _mark_welcome_message(message=message, source=source, job=job)
    return {"status": "completed", "engaged": True, "message_id": str(message.id)}
