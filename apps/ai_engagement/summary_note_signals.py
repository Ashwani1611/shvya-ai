"""Keep the latest AI conversation summary visible in Lead Notes.

InternalConversationSummary remains the versioned source of truth used by the AI
runtime. This signal mirrors only the currently published text into one system
LeadNote so CRM users can see both the conversation summary and the independent
qualification summary in the Notes surface without creating a new note on every
refresh.
"""

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.ai_engagement.models import InternalConversationSummary
from apps.ai_engagement.services.summary_limits import compact
from apps.crm.models import LeadNote
from services.crm_activity_service import record_note_added


CONVERSATION_SUMMARY_HEADER = "<AI Conversation Summary>"
CONVERSATION_SUMMARY_LIMIT = 1500


@receiver(
    post_save,
    sender=InternalConversationSummary,
    dispatch_uid="ai_conversation_summary_note_mirror",
)
def mirror_conversation_summary_to_note(sender, instance, created, **kwargs):
    if not instance.is_active:
        return

    summary = compact(instance.summary or "", CONVERSATION_SUMMARY_LIMIT)
    if not summary:
        return

    rendered = f"{CONVERSATION_SUMMARY_HEADER}\n{summary}"

    with transaction.atomic():
        note = (
            LeadNote.objects.select_for_update()
            .filter(
                lead_id=instance.lead_id,
                note_type="system",
                note__startswith=CONVERSATION_SUMMARY_HEADER,
            )
            .order_by("-created_at", "-id")
            .first()
        )

        if note is not None:
            if note.note == rendered:
                return
            note.note = rendered
            note.save(update_fields=["note", "updated_at"])
            return

        note = LeadNote.objects.create(
            lead_id=instance.lead_id,
            created_by=instance.created_by,
            note=rendered,
            note_type="system",
        )
        record_note_added(
            lead=instance.lead,
            actor=instance.created_by,
            note=note,
        )