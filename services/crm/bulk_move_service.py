"""Redis-backed state for large CRM bulk lead moves."""

import math
import uuid

from django.core.cache import cache


BULK_MOVE_JOB_TIMEOUT = 24 * 60 * 60
BULK_MOVE_JOB_PREFIX = "shvya:crm_bulk_move:"
BULK_MOVE_IDS_PREFIX = "shvya:crm_bulk_move_ids:"
BULK_MOVE_LOCK_PREFIX = "shvya:crm_bulk_move_lock:"
BULK_MOVE_LOCK_TIMEOUT = 5 * 60
BULK_MOVE_ID_CHUNK_SIZE = 500


def _ids_key(job_id, chunk_index):
    return f"{BULK_MOVE_IDS_PREFIX}{job_id}:{chunk_index}"


def create_bulk_move_job(*, organization_id, actor_id, lead_ids, selection_scope,
                         source_pipeline_id, source_stage_id, target_pipeline_id,
                         target_stage_id):
    job_id = str(uuid.uuid4())
    frozen_ids = [str(lead_id) for lead_id in lead_ids]
    chunk_count = math.ceil(len(frozen_ids) / BULK_MOVE_ID_CHUNK_SIZE)
    written_keys = []

    try:
        for chunk_index in range(chunk_count):
            start = chunk_index * BULK_MOVE_ID_CHUNK_SIZE
            end = start + BULK_MOVE_ID_CHUNK_SIZE
            key = _ids_key(job_id, chunk_index)
            cache.set(
                key,
                frozen_ids[start:end],
                timeout=BULK_MOVE_JOB_TIMEOUT,
            )
            written_keys.append(key)

        payload = {
            "status": "queued",
            "organization_id": str(organization_id),
            "actor_id": str(actor_id),
            "selection_scope": selection_scope,
            "source_pipeline_id": str(source_pipeline_id),
            "source_stage_id": str(source_stage_id) if source_stage_id else "",
            "target_pipeline_id": str(target_pipeline_id),
            "target_stage_id": str(target_stage_id),
            "processed": 0,
            "total": len(frozen_ids),
            "lead_id_chunks": chunk_count,
            "moved_count": 0,
            "skipped_count": 0,
            "message": "",
            "generation": 0,
        }
        save_bulk_move_job(job_id, payload)
        return job_id, payload
    except Exception:
        if written_keys:
            cache.delete_many(written_keys)
        raise


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


def get_bulk_move_id_slice(job_id, start, end):
    """Return only the frozen lead IDs needed for one bounded worker slice."""
    start = max(int(start or 0), 0)
    end = max(int(end or 0), start)
    if end <= start:
        return []

    first_chunk = start // BULK_MOVE_ID_CHUNK_SIZE
    last_chunk = (end - 1) // BULK_MOVE_ID_CHUNK_SIZE
    keys = [
        _ids_key(job_id, chunk_index)
        for chunk_index in range(first_chunk, last_chunk + 1)
    ]
    chunks = cache.get_many(keys)
    if any(key not in chunks for key in keys):
        return None

    flattened = []
    for key in keys:
        flattened.extend(chunks[key])

    base = first_chunk * BULK_MOVE_ID_CHUNK_SIZE
    selected = flattened[start - base:end - base]
    return selected if len(selected) == (end - start) else None


def delete_bulk_move_ids(job_id, chunk_count):
    chunk_count = max(int(chunk_count or 0), 0)
    if not chunk_count:
        return
    cache.delete_many(
        [_ids_key(job_id, chunk_index) for chunk_index in range(chunk_count)]
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
