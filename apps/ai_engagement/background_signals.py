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
        lambda lead_id=lead_id: queue_background_enrichment(lead_id=lead_id),
        robust=True,
    )


@receiver(post_save, sender=WhatsAppMessage)
def remember_ai_qualification_question(sender, instance, created, **kwargs):
    """Persist only the application-selected active requirement after AI queues it."""
    if not instance.lead_id:
        return
    if instance.direction != WhatsAppMessage.Direction.OUTBOUND:
        return
    payload = instance.raw_payload if isinstance(instance.raw_payload, dict) else {}
    ai_meta = payload.get("shvya_ai")
    if not isinstance(ai_meta, dict):
        return
    if (
        not ai_meta.get("next_requirement_id")
        and str(ai_meta.get("reason") or "").strip().upper() != "QUALIFICATION_NEXT"
    ):
        return

    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.organization_profile import compile_org_ai_profile
    from apps.ai_engagement.services.qualification_state import (
        next_requirement,
        record_last_asked_requirement,
        requirements_for_lead,
        state_for_lead,
    )
    from apps.crm.models import Lead

    # CRM actions may have updated a separately locked Lead instance immediately
    # before this message was queued. Re-read the authoritative lead and pinned
    # qualification flow rather than using message-time cached state.
    lead = Lead.objects.select_related("pipeline", "stage").get(
        pk=instance.lead_id,
        organization=instance.organization,
    )
    org_info = OrgInfo.objects.filter(organization=instance.organization).first()
    profile = compile_org_ai_profile(
        organization_name=instance.organization.name,
        org_info=org_info,
    )
    configured = profile.get("qualification", {}).get("requirements", [])
    requirements = requirements_for_lead(lead, configured)
    if not requirements:
        return

    state = state_for_lead(lead, requirements=requirements)
    selected = next_requirement(requirements, state.get("requirement_states", {}))
    if selected is None:
        return

    selected_id = str(selected.get("id") or "").strip()
    requested_id = str(ai_meta.get("next_requirement_id") or "").strip()
    # Never let an LLM-selected stale/earlier id become the persisted active
    # question. The backend's first unresolved requirement is authoritative.
    requirement_id = selected_id if requested_id != selected_id else requested_id
    if requirement_id:
        record_last_asked_requirement(
            lead,
            requirement_id,
            requirements=requirements,
        )
