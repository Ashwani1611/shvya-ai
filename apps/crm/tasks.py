"""Background processing for large CRM imports."""

import logging

from celery import shared_task
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from apps.crm.models import (
    AttributeDefinition,
    Lead,
    LeadActivity,
    Pipeline,
    Stage,
)
from apps.crm.models.lead import normalize_phone
from apps.organizations.models import Organization
from services.crm.lead_import_service import (
    delete_import_state,
    get_import_job,
    get_import_state,
    normalize_import_phone,
    release_import_job,
    save_import_job,
)

logger = logging.getLogger(__name__)

IMPORT_BATCH_SIZE = 250


def _prepare_import_row(row, mapping, definitions):
    """Normalize one spreadsheet row without doing database work."""
    name = (row.get(mapping["name"]) or "").strip()
    raw_phone = (row.get(mapping["phone"]) or "").strip()
    email = (row.get(mapping.get("email")) or "").strip()

    if not name or not raw_phone:
        raise ValidationError("Name and phone are required.")
    if len(name) > Lead._meta.get_field("name").max_length:
        raise ValidationError("Lead name is too long.")
    if email:
        validate_email(email)
        if len(email) > Lead._meta.get_field("email").max_length:
            raise ValidationError("Email address is too long.")

    phone = normalize_phone(normalize_import_phone(raw_phone))
    attributes = {
        definition.key: value
        for definition in definitions
        if (column := mapping.get(definition.key))
        if (value := (row.get(column) or "").strip())
    }
    return {
        "name": name,
        "phone": phone,
        "email": email,
        "attributes": attributes,
    }


def _created_activity(lead, organization, pipeline, stage):
    """Build the permanent CRM history entry without firing lead save signals."""
    return LeadActivity(
        lead=lead,
        organization=organization,
        topic=LeadActivity.Topic.LEAD_CREATED,
        actor=None,
        actor_name="",
        new_pipeline=pipeline,
        new_pipeline_name=pipeline.name,
        new_stage=stage,
        new_stage_name=stage.name,
        details={
            "lead_name": lead.name,
            "email": lead.email or "",
            "phone": lead.phone or "",
            "lead_source": "csv_import",
        },
    )


def _process_import_batch(*, organization, pipeline, stage, import_mode, prepared):
    """Persist one bounded batch and deliberately avoid per-lead signal fan-out."""
    phones = [item["phone"] for item in prepared]
    now = timezone.now()

    created = 0
    updated = 0
    skipped = 0

    with transaction.atomic():
        existing_by_phone = {
            lead.phone: lead
            for lead in (
                Lead.objects.select_for_update()
                .filter(organization=organization, phone__in=phones)
            )
        }

        new_leads = []
        updates_by_id = {}

        for item in prepared:
            existing = existing_by_phone.get(item["phone"])
            if existing is None:
                new_leads.append(
                    Lead(
                        organization=organization,
                        pipeline=pipeline,
                        stage=stage,
                        name=item["name"],
                        phone=item["phone"],
                        email=item["email"],
                        attributes=item["attributes"],
                        lead_source="csv_import",
                    )
                )
                continue

            if import_mode == "new_only":
                skipped += 1
                continue

            routing_changed = (
                existing.pipeline_id != pipeline.id
                or existing.stage_id != stage.id
            )
            existing.pipeline = pipeline
            existing.stage = stage
            if routing_changed:
                existing.stage_entered_at = now
            existing.name = item["name"] or existing.name
            if item["email"]:
                existing.email = item["email"]
            if item["attributes"]:
                existing.attributes = {
                    **(existing.attributes or {}),
                    **item["attributes"],
                }
            existing.updated_at = now
            updates_by_id[existing.pk] = existing
            updated += 1

        if new_leads:
            # UUID primary keys are assigned before INSERT. ignore_conflicts
            # handles a concurrent lead creation without failing the whole batch.
            Lead.objects.bulk_create(
                new_leads,
                batch_size=IMPORT_BATCH_SIZE,
                ignore_conflicts=True,
            )
            inserted_ids = set(
                Lead.objects.filter(pk__in=[lead.pk for lead in new_leads])
                .values_list("pk", flat=True)
            )
            inserted = [lead for lead in new_leads if lead.pk in inserted_ids]
            created = len(inserted)
            skipped += len(new_leads) - created

            LeadActivity.objects.bulk_create(
                [
                    _created_activity(lead, organization, pipeline, stage)
                    for lead in inserted
                ],
                batch_size=IMPORT_BATCH_SIZE,
            )

        if updates_by_id:
            Lead.objects.bulk_update(
                list(updates_by_id.values()),
                [
                    "pipeline",
                    "stage",
                    "stage_entered_at",
                    "name",
                    "email",
                    "attributes",
                    "updated_at",
                ],
                batch_size=IMPORT_BATCH_SIZE,
            )

    return {
        "created_count": created,
        "updated_count": updated,
        "skipped_count": skipped,
    }


@shared_task(
    name="crm.import_leads",
    acks_late=True,
    reject_on_worker_lost=True,
)
def import_leads_task(import_token, organization_id, import_mode):
    """Import leads in bounded batches on the ingestion queue.

    Spreadsheet imports intentionally do not emit Lead post-save signals. This
    prevents a large file from fanning out into workflows, Hosted WhatsApp
    refreshes, webhook deliveries, or welcome messages while preserving the
    permanent Lead Created CRM activity.
    """
    state = get_import_state(import_token)
    if not state or state.get("organization_id") != str(organization_id):
        save_import_job(
            import_token,
            {
                "status": "failed",
                "organization_id": str(organization_id),
                "message": "Import session expired. Upload the file again.",
            },
        )
        release_import_job(import_token)
        return

    total = len(state["rows"])
    previous_job = get_import_job(import_token) or {}
    can_resume = previous_job.get("status") == "running"
    next_index = int(previous_job.get("next_index") or 0) if can_resume else 0
    next_index = min(max(next_index, 0), total)

    count_keys = (
        "created_count",
        "updated_count",
        "skipped_count",
        "invalid_count",
    )
    counts = {
        key: int(previous_job.get(key) or 0) if can_resume else 0
        for key in count_keys
    }

    def update_status(status, processed, message="", checkpoint=None):
        payload = {
            "status": status,
            "organization_id": str(organization_id),
            "processed": processed,
            "total": total,
            "message": message,
            "import_mode": import_mode,
            **counts,
        }
        if checkpoint is not None:
            payload["next_index"] = checkpoint
        save_import_job(import_token, payload)

    try:
        organization = Organization.objects.get(pk=organization_id)
        pipeline = Pipeline.objects.get(
            pk=state["pipeline_id"],
            organization=organization,
            is_active=True,
        )
        stage = Stage.objects.get(
            pk=state["stage_id"],
            pipeline=pipeline,
            is_active=True,
        )
        mapping = state["mapping"]
        definitions = list(
            AttributeDefinition.objects.filter(
                organization=organization,
                is_active=True,
            )
        )

        update_status("running", next_index, checkpoint=next_index)

        for start in range(next_index, total, IMPORT_BATCH_SIZE):
            end = min(start + IMPORT_BATCH_SIZE, total)
            prepared = []

            for row in state["rows"][start:end]:
                try:
                    prepared.append(
                        _prepare_import_row(row, mapping, definitions)
                    )
                except ValidationError:
                    counts["invalid_count"] += 1

            if prepared:
                result = _process_import_batch(
                    organization=organization,
                    pipeline=pipeline,
                    stage=stage,
                    import_mode=import_mode,
                    prepared=prepared,
                )
                for key, value in result.items():
                    counts[key] += value

            # Persist a checkpoint only after the DB batch commits. If a worker
            # is lost, Celery redelivers the task and it resumes from here.
            update_status("running", end, checkpoint=end)

        update_status("completed", total, checkpoint=total)
        delete_import_state(import_token)
    except Exception:
        logger.exception(
            "Lead import failed: token=%s organization=%s",
            import_token,
            organization_id,
        )
        update_status(
            "failed",
            int(previous_job.get("next_index") or 0)
            if can_resume
            else sum(counts.values()),
            (
                "Import stopped. Reopen the import and try again; existing "
                "leads will be skipped when using New leads only."
            ),
        )
        raise
    finally:
        release_import_job(import_token)
