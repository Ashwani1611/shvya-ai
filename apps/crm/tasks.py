"""Background processing for large CRM imports."""

import logging
import uuid

from celery import shared_task
from celery.exceptions import Retry
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
from apps.accounts.models import User
from apps.crm.models.lead import normalize_phone
from apps.organizations.models import Organization
from services.crm.bulk_move_service import (
    acquire_bulk_move_lock,
    delete_bulk_move_ids,
    get_bulk_move_id_slice,
    get_bulk_move_job,
    refresh_bulk_move_lock,
    release_bulk_move_lock,
    save_bulk_move_job,
)
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
BULK_MOVE_BATCH_SIZE = 100
BULK_MOVE_BATCHES_PER_TASK = 5


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
    processed_index = next_index

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
            processed_index = end
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
            processed_index,
            (
                "Import stopped. Reopen the import and try again; existing "
                "leads will be skipped when using New leads only."
            ),
        )
        raise
    finally:
        release_import_job(import_token)


@shared_task(
    bind=True,
    name="crm.bulk_move_leads",
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=None,
)
def bulk_move_leads_task(self, job_id, generation=0):
    """Move a large frozen lead selection in bounded resumable task slices.

    A 10k+ move must not occupy one Celery delivery for the entire job. Each
    invocation processes at most a few database batches, checkpoints committed
    progress, then hands the next generation back to the ingestion queue.
    """
    from services.crm.lead_transition import (
        move_lead_to_pipeline_stage,
        move_lead_to_stage,
    )

    job = get_bulk_move_job(job_id)
    if not job:
        logger.warning("Bulk move job expired before execution: %s", job_id)
        return
    if job.get("status") == "completed":
        return

    generation = int(generation or 0)
    current_generation = int(job.get("generation") or 0)
    if generation < current_generation:
        # A stale redelivery after a newer slice has already been dispatched.
        return
    if generation > current_generation:
        # The successor raced the Redis checkpoint by a moment. Retry instead
        # of dropping it and leaving the large job stranded.
        raise self.retry(countdown=2)

    lock_token = str(uuid.uuid4())
    if not acquire_bulk_move_lock(job_id, lock_token):
        # Only one consumer may advance a job cursor. This also prevents an
        # acks_late redelivery from duplicating activity/workflow events.
        raise self.retry(countdown=2)

    try:
        # Re-read after acquiring the lock because another delivery may have
        # advanced the cursor while this task was waiting.
        job = get_bulk_move_job(job_id)
        if not job:
            logger.warning("Bulk move job expired while waiting for lock: %s", job_id)
            return
        if job.get("status") == "completed":
            return

        current_generation = int(job.get("generation") or 0)
        if generation < current_generation:
            return
        if generation > current_generation:
            raise self.retry(countdown=2)

        total = max(int(job.get("total") or 0), 0)
        processed = min(max(int(job.get("processed") or 0), 0), total)
        moved_count = int(job.get("moved_count") or 0)
        skipped_count = int(job.get("skipped_count") or 0)

        def checkpoint(status, message=""):
            nonlocal job
            job = {
                **job,
                "status": status,
                "processed": processed,
                "total": total,
                "moved_count": moved_count,
                "skipped_count": skipped_count,
                "message": message,
                "generation": current_generation,
            }
            save_bulk_move_job(job_id, job)

        organization = Organization.objects.get(pk=job["organization_id"])
        actor = User.objects.filter(
            pk=job["actor_id"],
            organization=organization,
        ).first()
        source_pipeline = Pipeline.objects.get(
            pk=job["source_pipeline_id"],
            organization=organization,
        )
        target_pipeline = Pipeline.objects.get(
            pk=job["target_pipeline_id"],
            organization=organization,
            is_active=True,
        )
        target_stage = Stage.objects.get(
            pk=job["target_stage_id"],
            pipeline=target_pipeline,
            is_active=True,
        )
        selection_scope = job.get("selection_scope") or "ids"
        source_stage_id = str(job.get("source_stage_id") or "")

        checkpoint("running")

        slice_end = min(
            processed + (BULK_MOVE_BATCH_SIZE * BULK_MOVE_BATCHES_PER_TASK),
            total,
        )
        for start in range(processed, slice_end, BULK_MOVE_BATCH_SIZE):
            end = min(start + BULK_MOVE_BATCH_SIZE, slice_end)
            batch_ids = get_bulk_move_id_slice(job_id, start, end)
            if batch_ids is None:
                raise RuntimeError(
                    "Bulk move frozen lead selection expired before processing completed."
                )
            batch_moved = 0
            batch_skipped = 0

            # Lock only this bounded batch. A worker failure rolls the entire
            # uncheckpointed batch back, while other CRM work stays responsive.
            # Progress counters are promoted only after the transaction commits.
            with transaction.atomic():
                leads = {
                    str(lead.pk): lead
                    for lead in Lead.objects.select_for_update(of=("self",))
                    .filter(
                        organization=organization,
                        pk__in=batch_ids,
                    )
                    .select_related("pipeline", "stage", "organization")
                }

                for lead_id in batch_ids:
                    lead = leads.get(str(lead_id))
                    if lead is None:
                        batch_skipped += 1
                        continue

                    # Redelivery is idempotent if an earlier checkpoint was
                    # persisted after this lead reached the requested target.
                    if (
                        lead.pipeline_id == target_pipeline.id
                        and lead.stage_id == target_stage.id
                    ):
                        batch_moved += 1
                        continue

                    if lead.pipeline_id != source_pipeline.id:
                        batch_skipped += 1
                        continue
                    if (
                        selection_scope != "pipeline"
                        and source_stage_id
                        and str(lead.stage_id) != source_stage_id
                    ):
                        batch_skipped += 1
                        continue

                    if lead.pipeline_id == target_pipeline.id:
                        move_lead_to_stage(
                            lead=lead,
                            stage=target_stage,
                            actor=actor,
                        )
                    else:
                        move_lead_to_pipeline_stage(
                            lead=lead,
                            pipeline=target_pipeline,
                            stage=target_stage,
                            actor=actor,
                        )
                    batch_moved += 1

            moved_count += batch_moved
            skipped_count += batch_skipped
            processed = end
            checkpoint("running")
            if not refresh_bulk_move_lock(job_id, lock_token):
                raise RuntimeError("Bulk move worker lost its job lock.")

        if processed >= total:
            checkpoint(
                "completed",
                "" if not skipped_count else (
                    f"{skipped_count} lead(s) were skipped because they changed "
                    "or were deleted before processing."
                ),
            )
            delete_bulk_move_ids(job_id, job.get("lead_id_chunks"))
            return

        # Publish the successor before advancing the generation. If it starts
        # immediately it will retry until this checkpoint is visible. This order
        # also means a worker crash can never leave a generation saved in Redis
        # without a corresponding queued task.
        next_generation = current_generation + 1
        bulk_move_leads_task.apply_async(
            args=[job_id, next_generation],
            queue="ingestion",
            countdown=1,
        )
        current_generation = next_generation
        checkpoint("running")
    except Retry:
        raise
    except Exception:
        logger.exception("Bulk CRM lead move failed: job=%s", job_id)
        job = get_bulk_move_job(job_id) or job
        job = {
            **job,
            "status": "failed",
            "message": (
                "Bulk lead move stopped safely. Refresh the CRM to see completed "
                "batches before retrying."
            ),
        }
        save_bulk_move_job(job_id, job)
        raise
    finally:
        release_bulk_move_lock(job_id, lock_token)

