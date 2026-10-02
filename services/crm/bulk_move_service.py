"""Redis-backed state for large CRM bulk lead moves."""

import uuid

from django.core.cache import cache


BULK_MOVE_JOB_TIMEOUT = 24 * 60 * 60
BULK_MOVE_JOB_PREFIX = "shvya:crm_bulk_move:"
BULK_MOVE_LOCK_PREFIX = "shvya:crm_bulk_move_lock:"
BULK_MOVE_LOCK_TIMEOUT = 5 * 60


def create_bulk_move_job(*, organization_id, actor_id, lead_ids, selection_scope,
                         source_pipeline_id, source_stage_id, target_pipeline_id,
                         target_stage_id):
    job_id = str(uuid.uuid4())
    payload = {
        "status": "queued",
        "organization_id": str(organization_id),
        "actor_id": str(actor_id),
        "lead_ids": [str(lead_id) for lead_id in lead_ids],
        "selection_scope": selection_scope,
        "source_pipeline_id": str(source_pipeline_id),
        "source_stage_id": str(source_stage_id) if source_stage_id else "",
        "target_pipeline_id": str(target_pipeline_id),
        "target_stage_id": str(target_stage_id),
        "processed": 0,
        "total": len(lead_ids),
        "moved_count": 0,
        "skipped_count": 0,
        "message": "",
        "generation": 0,
    }
    save_bulk_move_job(job_id, payload)
    return job_id, payload


def get_bulk_move_job(job_id):
    if not job_id:
        return None
    return cache.get(f"{BULK_MOVE_JOB_PREFIX}{job_id}")


def save_bulk_move_job(job_id, payload):
    cache.set(
        f"{BULK_MOVE_JOB_PREFIX}{job_id}",
        payload,
        timeout=BULK_MOVE_JOB_TIMEOUT,
    )


def acquire_bulk_move_lock(job_id, token):
    """Acquire a short-lived single-consumer lock for one bulk-move job."""
    if not job_id or not token:
        return False
    return cache.add(
        f"{BULK_MOVE_LOCK_PREFIX}{job_id}",
        token,
        timeout=BULK_MOVE_LOCK_TIMEOUT,
    )


def refresh_bulk_move_lock(job_id, token):
    """Extend a lock held by this worker after each committed batch."""
    key = f"{BULK_MOVE_LOCK_PREFIX}{job_id}"
    if cache.get(key) != token:
        return False
    cache.set(key, token, timeout=BULK_MOVE_LOCK_TIMEOUT)
    return True


def release_bulk_move_lock(job_id, token):
    """Release only the lock still owned by this worker."""
    key = f"{BULK_MOVE_LOCK_PREFIX}{job_id}"
    if cache.get(key) == token:
        cache.delete(key)
