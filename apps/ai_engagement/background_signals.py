"""Non-blocking internal AI enrichment and qualification-state hooks."""

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.ai_engagement.services.background_enrichment import (
    queue_background_enrichment,
)
from apps.channels.models import WhatsAppMessage


@receiver(post_save, sender=WhatsAppMessage)
def queue_internal_ai_enrichment(sender, instance, created, **kwargs):
    if not created or not instance.lead_id:
        return
    if instance.direction != WhatsAppMessage.Direction.INBOUND:
        return

    lead_id = str(instance.lead_id)
    transaction.on_commit(
        lambda lead_id=lead_id: queue_background_enrichment(
            lead_id=lead_id,
        )
    )


@receiver(post_save, sender=WhatsAppMessage)
def remember_ai_qualification_question(sender, instance, created, **kwargs):
    """Persist the exact application-selected requirement after AI queues it."""
    if not instance.lead_id:
        return
    if instance.direction != WhatsAppMessage.Direction.OUTBOUND:
        return
    payload = instance.raw_payload if isinstance(instance.raw_payload, dict) else {}
    ai_meta = payload.get("shvya_ai")
    if not isinstance(ai_meta, dict):
        return
    if str(ai_meta.get("reason") or "").strip().upper() != "QUALIFICATION_NEXT":
        return

    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.organization_profile import (
        compile_org_ai_profile,
    )
    from apps.ai_engagement.services.qualification_state import (
        next_requirement,
        record_last_asked_requirement,
        state_for_lead,
    )

    lead = instance.lead
    org_info = OrgInfo.objects.filter(organization=instance.organization).first()
    profile = compile_org_ai_profile(
        organization_name=instance.organization.name,
        org_info=org_info,
    )
    requirements = profile.get("qualification", {}).get("requirements", [])
    if not requirements:
        return
    state = state_for_lead(lead, requirements=requirements)
    selected = next_requirement(requirements, state.get("requirement_states", {}))
    if selected is None:
        return
    requirement_id = str(selected.get("id") or "").strip()
    if requirement_id:
        record_last_asked_requirement(
            lead,
            requirement_id,
            requirements=requirements,
        )
