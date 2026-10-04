"""Instagram CRM lead linking, auto-creation, and phone capture.

Instagram participant IDs are the conversation identity. A phone number is an
optional CRM attribute and must never be required to create or engage an
Instagram lead.
"""
from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.channels.instagram_models import InstagramConversation
from apps.crm.models import Lead, Pipeline
from apps.crm.models.lead import normalize_phone
from services.crm.lead_filter_service import accessible_pipelines
from services.crm_activity_service import record_lead_created


_PHONE_CANDIDATE = re.compile(r"(?<!\w)(\+?\d[\d\s().-]{6,}\d)(?!\w)")

_PHONE_CONTEXT_TERMS = (
    "phone",
    "mobile",
    "number",
    "contact",
    "call",
    "whatsapp",
    "reach me",
    "reach us",
    "tel",
    "telephone",
)


def _preferred_pipeline_stage(organization):
    pipelines = Pipeline.objects.filter(
        organization=organization,
        is_active=True,
    )
    pipeline = (
        pipelines.filter(name__iexact="Leads").order_by("created_at", "id").first()
        or pipelines.filter(name__iexact="Lead").order_by("created_at", "id").first()
        or pipelines.order_by("created_at", "id").first()
    )
    if pipeline is None:
        return None, None

    stages = pipeline.stages.filter(is_active=True)
    stage = (
        stages.filter(name__iexact="New Lead").order_by("display_order", "id").first()
        or stages.filter(name__iexact="New Leads").order_by("display_order", "id").first()
        or stages.order_by("display_order", "id").first()
    )
    return pipeline, stage


def _instagram_username(conversation):
    return str(
        getattr(conversation, "participant_username", "") or ""
    ).strip().lstrip("@")[:150]


def _lead_name(conversation, supplied=""):
    return (
        _instagram_username(conversation)
        or str(supplied or "").strip()
        or str(conversation.participant_name or "").strip()
        or "Instagram user"
    )[:150]


def sync_instagram_lead_name(conversation):
    """Keep Instagram-created CRM lead names aligned to the Instagram username."""
    if not getattr(conversation, "lead_id", None):
        return None

    lead = getattr(conversation, "lead", None)
    if lead is None:
        lead = Lead.objects.filter(
            pk=conversation.lead_id,
            organization_id=conversation.organization_id,
        ).first()
    if lead is None or lead.lead_source != "instagram":
        return lead

    username = _instagram_username(conversation)
    if username and lead.name != username:
        lead.name = username
        lead.save(update_fields=["name", "updated_at"])
    return lead


def normalize_instagram_phone(value, *, country_code=""):
    """Normalize an explicitly shared phone without guessing a country.

    Numbers already carrying a plus prefix are accepted directly. A local-format
    number is accepted only when the lead pipeline has a country code configured.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""

    if raw.startswith("+"):
        normalized = normalize_phone(raw)
    else:
        digits = "".join(character for character in raw if character.isdigit())
        country_digits = "".join(
            character for character in str(country_code or "") if character.isdigit()
        )
        if not country_digits:
            raise ValidationError(
                {"phone": "Add the country code, for example +91 9876543210."}
            )
        if digits.startswith(country_digits):
            normalized = normalize_phone(f"+{digits}")
        else:
            normalized = normalize_phone(f"+{country_digits}{digits}")

    if len("".join(character for character in normalized if character.isdigit())) > 15:
        raise ValidationError({"phone": "Phone number is too long."})
    return normalized


def extract_instagram_phone(text, *, country_code=""):
    """Return a phone only when the DM supplies credible phone evidence.

    A bare number (for example a customer replying only with their mobile) is
    accepted. An embedded local-format number needs nearby phone/contact
    language so budgets, order IDs, dates, and other numeric values are not
    silently written into the CRM phone field. International +numbers are
    already self-identifying and can be embedded without a cue.
    """
    source = str(text or "")
    normalized_source = source.casefold()

    for match in _PHONE_CANDIDATE.finditer(source):
        raw = match.group(1).strip()
        remaining = (source[: match.start()] + source[match.end() :]).strip(
            " \t\r\n,.;:()[]{}-"
        )
        standalone = not remaining
        nearby = normalized_source[
            max(0, match.start() - 40) : min(len(source), match.end() + 40)
        ]
        has_context = any(term in nearby for term in _PHONE_CONTEXT_TERMS)
        if not raw.startswith("+") and not standalone and not has_context:
            continue

        try:
            return normalize_instagram_phone(
                raw,
                country_code=country_code,
            )
        except ValidationError:
            continue
    return ""


@transaction.atomic
def ensure_instagram_lead(*, conversation_id):
    """Auto-create and link a phone-optional CRM lead for a live Instagram DM."""
    conversation = (
        InstagramConversation.objects.select_for_update()
        .select_related("organization", "account")
        .get(pk=conversation_id)
    )
    if conversation.lead_id:
        from apps.ai_engagement.services.intent_score import persist_intent_score

        lead = sync_instagram_lead_name(conversation) or conversation.lead
        persist_intent_score(lead=lead)
        return conversation, lead

    from apps.channels.services.instagram_automation import get_settings

    if not get_settings(organization_id=conversation.organization_id)["auto_lead_creation"]:
        return conversation, None

    pipeline, stage = _preferred_pipeline_stage(conversation.organization)
    if pipeline is None or stage is None:
        # The DM remains visible in the Instagram inbox. AI cannot engage until
        # the workspace has an active CRM pipeline/stage to own the lead.
        return conversation, None

    lead = Lead(
        organization=conversation.organization,
        pipeline=pipeline,
        stage=stage,
        name=_lead_name(conversation),
        phone="",
        lead_source="instagram",
    )
    lead.full_clean()
    lead.save()
    record_lead_created(lead=lead, actor=None)

    conversation.lead = lead
    conversation.save(update_fields=["lead", "updated_at"])

    from apps.ai_engagement.services.intent_score import persist_intent_score

    persist_intent_score(lead=lead)
    return conversation, lead


@transaction.atomic
def map_instagram_phone_from_message(*, lead_id, text):
    """Map a phone explicitly shared in an Instagram DM onto its CRM lead."""
    lead = (
        Lead.objects.select_for_update()
        .select_related("pipeline")
        .filter(pk=lead_id, lead_source="instagram")
        .first()
    )
    if lead is None:
        return ""

    phone = extract_instagram_phone(
        text,
        country_code=getattr(lead.pipeline, "country_code", ""),
    )
    if not phone or phone == lead.phone:
        return phone

    # Never steal a phone already owned by another CRM lead. The Instagram
    # participant ID remains the authoritative identity for this conversation.
    if Lead.objects.filter(
        organization_id=lead.organization_id,
        phone=phone,
    ).exclude(pk=lead.pk).exists():
        return ""

    lead.phone = phone
    lead.full_clean()
    lead.save(update_fields=["phone", "updated_at"])
    return phone


@transaction.atomic
def link_instagram_lead(*, user, conversation_id, phone="", name="", pipeline_id=""):
    """Manually link/create an Instagram lead; phone is optional."""
    conversation = (
        InstagramConversation.objects.select_for_update()
        .get(
            pk=conversation_id,
            organization=user.organization,
            account__organization=user.organization,
        )
    )
    if conversation.lead_id:
        if str(phone or "").strip():
            current_lead = Lead.objects.select_related("pipeline").get(
                pk=conversation.lead_id,
                organization=user.organization,
            )
            normalized = normalize_instagram_phone(
                phone,
                country_code=getattr(current_lead.pipeline, "country_code", ""),
            )
            if normalized != current_lead.phone:
                raise ValidationError(
                    "This Instagram conversation is already linked to another lead identity."
                )
        return conversation

    allowed = accessible_pipelines(user)
    pipeline = allowed.filter(pk=pipeline_id).first() if pipeline_id else None

    raw_phone = str(phone or "").strip()
    normalized_phone = ""
    if raw_phone:
        normalized_phone = normalize_instagram_phone(
            raw_phone,
            country_code=getattr(pipeline, "country_code", "") if pipeline else "",
        )

    lead = None
    if normalized_phone:
        lead = Lead.objects.filter(
            organization=user.organization,
            phone=normalized_phone,
        ).first()
        if lead and not allowed.filter(pk=lead.pipeline_id).exists():
            raise ValidationError("This lead is not in an accessible pipeline.")

    if lead is None:
        if not pipeline or not str(name or "").strip():
            raise ValidationError(
                "Choose a pipeline and enter a name to create this Instagram lead."
            )
        stage = pipeline.stages.filter(is_active=True).order_by(
            "display_order", "id"
        ).first()
        if not stage:
            raise ValidationError("This pipeline has no active stage.")

        lead = Lead(
            organization=user.organization,
            pipeline=pipeline,
            stage=stage,
            name=_lead_name(conversation, name),
            phone=normalized_phone,
            lead_source="instagram",
        )
        lead.full_clean()
        lead.save()
        record_lead_created(lead=lead, actor=user)

    conversation.lead = lead
    conversation.save(update_fields=["lead", "updated_at"])

    from apps.ai_engagement.services.intent_score import persist_intent_score

    persist_intent_score(lead=lead)
    return conversation
