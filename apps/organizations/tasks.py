"""Retry storage and hosted-session cleanup after tenant deletion commits."""

from celery import shared_task
from django.apps import apps
from django.db import models, transaction
from .models import OrganizationDeletionCleanup


@shared_task(name="organizations.cleanup_deleted")
def cleanup_deleted_organizations():
    # An outbox survives broker outages and process restarts. Beat retries it.
    for cleanup_id in list(
        OrganizationDeletionCleanup.objects.order_by("created_at").values_list(
            "pk", flat=True
        )[:20]
    ):
        with transaction.atomic():
            job = (
                OrganizationDeletionCleanup.objects.select_for_update(skip_locked=True)
                .filter(pk=cleanup_id)
                .first()
            )
            if job is None:
                continue
            files_remaining, sessions_remaining = [], []
            for session_id in job.hosted_session_ids:
                try:
                    from apps.channels.providers.whatsapp_web import (
                        WhatsAppWebClient,
                        WhatsAppWebGatewayError,
                    )

                    try:
                        WhatsAppWebClient().logout(session_id=session_id)
                    except WhatsAppWebGatewayError as exc:
                        if exc.status_code != 404:
                            raise
                except Exception:
                    sessions_remaining.append(session_id)
            for entry in job.files:
                try:
                    field = apps.get_model(entry["model"])._meta.get_field(
                        entry["field"]
                    )
                    # A reused asset still referenced by any live record must stay.
                    referenced = any(
                        model._base_manager.filter(
                            **{other.name: entry["name"]}
                        ).exists()
                        for model in apps.get_models()
                        for other in model._meta.fields
                        if isinstance(other, models.FileField)
                    )
                    if not referenced:
                        field.storage.delete(entry["name"])
                except Exception:
                    files_remaining.append(entry)
            if not files_remaining and not sessions_remaining:
                job.delete()
            else:
                job.files = files_remaining
                job.hosted_session_ids = sessions_remaining
                job.attempts += 1
                job.last_error = "External cleanup incomplete; automatic retry pending."
                job.save(
                    update_fields=[
                        "files",
                        "hosted_session_ids",
                        "attempts",
                        "last_error",
                    ]
                )
