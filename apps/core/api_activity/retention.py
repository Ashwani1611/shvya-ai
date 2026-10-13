"""Bounded retention shared by the operational command and scheduled task."""

from datetime import timedelta
from time import monotonic

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.utils import timezone


def prune_activities(*, days=None, batch_size=500, max_batches=20, dry_run=False, max_seconds=30):
    days = int(getattr(settings, "API_ACTIVITY_RETENTION_DAYS", 30)) if days is None else days
    if not 1 <= days <= 3650:
        raise ValueError("--days must be between 1 and 3650")
    if not 1 <= batch_size <= 2000 or not 1 <= max_batches <= 1000:
        raise ValueError("--batch-size must be 1–2000 and --max-batches must be 1–1000")
    model = apps.get_model("core", "APIActivity")
    cutoff = timezone.now() - timedelta(days=days)
    candidates = model.objects.filter(created_at__lt=cutoff).exclude(
        incident__status__in=("open", "acknowledged"),
    )
    if dry_run:
        return {"days": days, "eligible": candidates.count(), "deleted": 0, "dry_run": True}
    started = monotonic()
    removed = 0
    for _ in range(max_batches):
        if monotonic() - started >= max_seconds:
            break
        with transaction.atomic():
            # Investigation writes lock the same activity, so a concurrent
            # operator cannot pin a row after selection but before deletion.
            ids = list(candidates.select_for_update(of=("self",), skip_locked=True).order_by(
                "created_at", "id"
            ).values_list("pk", flat=True)[:batch_size])
            if not ids:
                break
            _, counts = model.objects.filter(pk__in=ids).delete()
            removed += counts.get("core.APIActivity", 0)
    return {"days": days, "deleted": removed, "dry_run": False, "has_more": candidates.exists()}
