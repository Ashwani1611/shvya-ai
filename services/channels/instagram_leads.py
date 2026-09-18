"""Explicit Instagram identity linking; names and provider IDs are never phone numbers."""
from django.core.exceptions import ValidationError
from django.db import transaction
from apps.channels.instagram_models import InstagramConversation
from apps.crm.models import Lead
from apps.crm.models.lead import normalize_phone
from services.crm.lead_filter_service import accessible_pipelines
from services.crm_activity_service import record_lead_created


@transaction.atomic
def link_instagram_lead(*, user, conversation_id, phone, name="", pipeline_id=""):
    conversation = InstagramConversation.objects.select_for_update().get(
        pk=conversation_id, organization=user.organization, account__organization=user.organization,
    )
    phone = normalize_phone(phone)
    allowed = accessible_pipelines(user)
    lead = Lead.objects.filter(organization=user.organization, phone=phone).first()
    if lead and not allowed.filter(pk=lead.pipeline_id).exists():
        raise ValidationError("This lead is not in an accessible pipeline.")
    if conversation.lead_id and (not lead or conversation.lead_id != lead.pk):
        raise ValidationError("This conversation is already linked to another lead. Its identity cannot be overwritten here.")
    if not lead:
        pipeline = allowed.filter(pk=pipeline_id).first() if pipeline_id else None
        if not pipeline or not name.strip():
            raise ValidationError("No lead has this phone number. Enter a name and choose a pipeline to create one.")
        stage = pipeline.stages.filter(is_active=True).order_by("display_order").first()
        if not stage:
            raise ValidationError("This pipeline has no active stage.")
        lead = Lead(organization=user.organization, pipeline=pipeline, stage=stage,
                    name=name.strip(), phone=phone, lead_source="instagram")
        lead.full_clean()
        lead.save()
        record_lead_created(lead=lead, actor=user)
    conversation.lead = lead
    conversation.save(update_fields=["lead", "updated_at"])
    from apps.ai_engagement.services.intent_score import persist_intent_score
    persist_intent_score(lead=lead)
    return conversation
