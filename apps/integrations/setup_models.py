"""Organization-owned onboarding evidence, separate from customer knowledge."""

import uuid

from django.conf import settings
from django.core.validators import MaxLengthValidator
from django.db import models


SETUP_INTAKE_SECTIONS = (
    "website", "brochures", "media", "offerings", "basics", "faqs", "team",
    "qualification", "handoff", "blacklist", "rules", "proof", "offers",
    "scripts", "other",
)
SETUP_INTAKE_KINDS = ("note", "question", "call", "attachment")
SETUP_INTAKE_ORIGINS = (
    "client_document", "call_export", "whatsapp_export", "ops_chat", "other",
)
SETUP_INTAKE_STATUSES = ("reported", "confirmed", "conflict", "open", "answered")
SETUP_INTAKE_HISTORY_LIMIT = 10


class OperationsIntakeEntry(models.Model):
    """Draft source evidence; never automatically indexed or customer-visible."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE,
        related_name="operations_intake_entries",
    )
    section = models.CharField(
        max_length=24, choices=[(value, value) for value in SETUP_INTAKE_SECTIONS],
    )
    kind = models.CharField(
        max_length=16, choices=[(value, value) for value in SETUP_INTAKE_KINDS],
    )
    external_id = models.CharField(max_length=120)
    origin = models.CharField(
        max_length=24, choices=[(value, value) for value in SETUP_INTAKE_ORIGINS],
    )
    source_id = models.CharField(max_length=160)
    source_ref = models.CharField(max_length=500)
    source_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=[(value, value) for value in SETUP_INTAKE_STATUSES],
    )
    body = models.TextField(validators=[MaxLengthValidator(12000)])
    is_active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)
    # Only the ten previous snapshots are retained, avoiding unbounded context
    # or JSON growth. The monotonically increasing revision tracks total edits.
    history = models.JSONField(
        default=list, blank=True, validators=[MaxLengthValidator(SETUP_INTAKE_HISTORY_LIMIT)],
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "section", "kind", "external_id"],
                name="ops_intake_org_source_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1), name="ops_intake_revision_positive",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "is_active", "id"],
                name="ops_intake_org_active_idx",
            ),
        ]
