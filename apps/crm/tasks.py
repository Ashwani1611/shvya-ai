"""Background processing for large CRM imports."""

import logging

from celery import shared_task
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.crm.models import AttributeDefinition, Lead, Pipeline, Stage
from apps.crm.models.lead import normalize_phone
from apps.organizations.models import Organization
from services.crm.lead_import_service import (
    delete_import_state,
    get_import_state,
    normalize_import_phone,
    release_import_job,
    save_import_job,
)
from services.crm.lead_service import create_lead

logger = logging.getLogger(__name__)


@shared_task(name="crm.import_leads")
def import_leads_task(import_token, organization_id, import_mode):
    """Process a wizard upload outside the HTTP timeout; never contact imported leads."""
    state = get_import_state(import_token)
    if not state or state.get("organization_id") != str(organization_id):
        save_import_job(import_token, {
            "status": "failed", "organization_id": str(organization_id),
            "message": "Import session expired. Upload the file again.",
        })
        release_import_job(import_token)
        return

    counts = {"created_count": 0, "updated_count": 0, "skipped_count": 0, "invalid_count": 0}
    total = len(state["rows"])

    def update_status(status, processed, message=""):
        save_import_job(import_token, {
            "status": status, "organization_id": str(organization_id),
            "processed": processed, "total": total, "message": message,
            "import_mode": import_mode, **counts,
        })

    try:
        organization = Organization.objects.get(pk=organization_id)
        pipeline = Pipeline.objects.get(
            pk=state["pipeline_id"], organization=organization, is_active=True,
        )
        stage = Stage.objects.get(
            pk=state["stage_id"], pipeline=pipeline, is_active=True,
        )
        mapping = state["mapping"]
        definitions = list(AttributeDefinition.objects.filter(
            organization=organization, is_active=True,
        ))
        update_status("running", 0)

        for index, row in enumerate(state["rows"], start=1):
            if index > 1 and (index - 1) % 100 == 0:
                update_status("running", index - 1)
            name = (row.get(mapping["name"]) or "").strip()
            raw_phone = (row.get(mapping["phone"]) or "").strip()
            email = (row.get(mapping.get("email")) or "").strip()
            if not name or not raw_phone:
                counts["invalid_count"] += 1
                continue

            try:
                phone = normalize_phone(normalize_import_phone(raw_phone))
                attributes = {
                    definition.key: value
                    for definition in definitions
                    if (column := mapping.get(definition.key))
                    if (value := (row.get(column) or "").strip())
                }
                with transaction.atomic():
                    existing = Lead.objects.filter(
                        organization=organization, phone=phone,
                    ).first()
                    if existing:
                        if import_mode == "new_only":
                            counts["skipped_count"] += 1
                            continue
                        existing.pipeline = pipeline
                        existing.stage = stage
                        existing.name = name or existing.name
                        if email:
                            existing.email = email
                        if attributes:
                            existing.attributes = {**(existing.attributes or {}), **attributes}
                        existing.full_clean()
                        existing.save()
                        counts["updated_count"] += 1
                    else:
                        create_lead(
                            organization=organization, pipeline=pipeline, stage=stage,
                            name=name, phone=phone, email=email, attributes=attributes,
                            lead_source="csv_import", send_welcome=False,
                        )
                        counts["created_count"] += 1
            except (ValidationError, IntegrityError):
                counts["invalid_count"] += 1

        update_status("completed", total)
        delete_import_state(import_token)
    except Exception:
        logger.exception("Lead import failed: token=%s organization=%s", import_token, organization_id)
        update_status("failed", sum(counts.values()), "Import stopped. Reopen the import and try again; existing leads will be skipped when using New leads only.")
        raise
    finally:
        release_import_job(import_token)
