"""
CRM Lead Activity Service

Centralized creation of permanent Lead Activity records.

Rules:
    - Activity belongs to the Lead.
    - Activity is never tied to the Lead's current stage/pipeline.
    - Historical pipeline/stage names are snapshotted.
    - Actor identity is snapshotted.
    - Views/actions should use this service instead of creating
      LeadActivity objects directly.
"""

from apps.crm.models import LeadActivity


def _actor_name(actor):
    """
    Return the best available historical display name for the actor.
    """

    if actor is None:
        return ""

    name = getattr(
        actor,
        "name",
        "",
    )

    if name:
        return name.strip()

    email = getattr(
        actor,
        "email",
        "",
    )

    return email.strip()


def create_lead_activity(
    *,
    lead,
    actor=None,
    topic,
    organization=None,
    old_pipeline=None,
    new_pipeline=None,
    old_stage=None,
    new_stage=None,
    details=None,
):
    """
    Create one permanent Lead Activity record.

    The activity is explicitly attached to the Lead and organization.
    Pipeline/stage values are stored both as relationships and as
    historical name snapshots.

    This function intentionally does not infer the old/new state from
    the Lead after a mutation. Callers must provide the correct state
    before the mutation is lost.
    """

    if organization is None:
        organization = lead.organization

    actor_name = _actor_name(
        actor
    )

    return LeadActivity.objects.create(
        lead=lead,
        organization=organization,
        topic=topic,
        actor=actor,
        actor_name=actor_name,

        old_pipeline=old_pipeline,
        old_pipeline_name=(
            old_pipeline.name
            if old_pipeline
            else ""
        ),

        new_pipeline=new_pipeline,
        new_pipeline_name=(
            new_pipeline.name
            if new_pipeline
            else ""
        ),

        old_stage=old_stage,
        old_stage_name=(
            old_stage.name
            if old_stage
            else ""
        ),

        new_stage=new_stage,
        new_stage_name=(
            new_stage.name
            if new_stage
            else ""
        ),

        details=details or {},
    )


def record_lead_created(
    *,
    lead,
    actor=None,
):
    """
    Record initial Lead creation.
    """

    return create_lead_activity(
        lead=lead,
        actor=actor,
        topic=LeadActivity.Topic.LEAD_CREATED,
        new_pipeline=lead.pipeline,
        new_stage=lead.stage,
        details={
            "lead_name": lead.name,
            "email": lead.email or "",
            "phone": lead.phone or "",
        },
    )


def record_lead_updated(
    *,
    lead,
    actor=None,
    changed_fields=None,
):
    """
    Record a normal Lead edit/update.

    changed_fields should contain structured information about the
    fields that changed, for example:

        {
            "name": {
                "old": "John",
                "new": "Jonathan",
            },
            "attributes": {
                "old": {...},
                "new": {...},
            },
        }
    """

    return create_lead_activity(
        lead=lead,
        actor=actor,
        topic=LeadActivity.Topic.LEAD_UPDATED,
        new_pipeline=lead.pipeline,
        new_stage=lead.stage,
        details={
            "changed_fields": (
                changed_fields or {}
            ),
        },
    )


def record_pipeline_changed(
    *,
    lead,
    actor=None,
    old_pipeline,
    new_pipeline,
    old_stage=None,
    new_stage=None,
):
    """
    Record a Pipeline transition.

    The stage context is also retained because pipeline and stage are
    related but distinct dimensions of Lead movement.
    """

    return create_lead_activity(
        lead=lead,
        actor=actor,
        topic=LeadActivity.Topic.PIPELINE_CHANGED,

        old_pipeline=old_pipeline,
        new_pipeline=new_pipeline,

        old_stage=old_stage,
        new_stage=new_stage,

        details={
            "movement_type": "pipeline_change",
        },
    )


def record_stage_changed(
    *,
    lead,
    actor=None,
    pipeline,
    old_stage,
    new_stage,
):
    """
    Record a Stage transition inside a Pipeline.
    """

    return create_lead_activity(
        lead=lead,
        actor=actor,
        topic=LeadActivity.Topic.STAGE_CHANGED,

        old_pipeline=pipeline,
        new_pipeline=pipeline,

        old_stage=old_stage,
        new_stage=new_stage,

        details={
            "movement_type": "stage_change",
        },
    )


def record_note_added(
    *,
    lead,
    actor=None,
    note,
):
    """
    Record creation of a manual/system note.
    """

    return create_lead_activity(
        lead=lead,
        actor=actor,
        topic=LeadActivity.Topic.NOTE_ADDED,

        new_pipeline=lead.pipeline,
        new_stage=lead.stage,

        details={
            "note_id": str(
                note.id
            ),
            "note_type": note.note_type,
        },
    )


def record_call_logged(
    *,
    lead,
    actor=None,
    call,
):
    """
    Record a logged Call.
    """

    return create_lead_activity(
        lead=lead,
        actor=actor,
        topic=LeadActivity.Topic.CALL_LOGGED,

        new_pipeline=lead.pipeline,
        new_stage=lead.stage,

        details={
            "call_id": str(
                call.id
            ),
            "call_name": call.call_name,
            "status": call.status,
            "duration_seconds": (
                call.duration_seconds
            ),
            "notes": call.notes or "",
            "called_at": (
                call.called_at.isoformat()
                if call.called_at
                else None
            ),
        },
    )




def record_reminder_created(
    *,
    lead,
    actor=None,
    reminder,
):
    """
    Record creation of a Reminder.
    """

    return create_lead_activity(
        lead=lead,
        actor=actor,
        topic=LeadActivity.Topic.REMINDER_CREATED,

        new_pipeline=lead.pipeline,
        new_stage=lead.stage,

        details={
            "reminder_id": str(
                reminder.id
            ),
            "title": reminder.title,
            "due_at": (
                reminder.due_at.isoformat()
                if reminder.due_at
                else None
            ),
        },
    )


def record_reminder_completed(
    *,
    lead,
    actor=None,
    reminder,
):
    """
    Record Reminder completion.
    """

    return create_lead_activity(
        lead=lead,
        actor=actor,
        topic=LeadActivity.Topic.REMINDER_COMPLETED,

        new_pipeline=lead.pipeline,
        new_stage=lead.stage,

        details={
            "reminder_id": str(
                reminder.id
            ),
            "title": reminder.title,
            "completed_at": (
                reminder.completed_at.isoformat()
                if reminder.completed_at
                else None
            ),
        },
    )