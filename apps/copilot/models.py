import uuid

from django.db import models


class CopilotLeadFlag(models.Model):
    """Cached Co-Pilot signal for one lead.

    Flags are intentionally persisted instead of computed during every page
    request.  The scanner in ``services.copilot_service`` refreshes these
    rows and the dashboard only reads the cache.
    """

    class FlagCode(models.TextChoices):
        REPLY_PENDING = "R1", "Reply Pending"
        NEW_LEAD_NO_CONTACT = "R2", "New Lead, No Contact"
        DELIVERY_FAILURE = "R3", "Delivery Failure / Delay"
        NO_CALLS_EVER = "C1", "No Calls Ever Made"
        CALL_GAP = "C2", "Call Gap"
        ALL_CALLS_NO_RESPONSE = "C3", "All Calls No Response"
        HIGH_INTENT_NO_ACTION = "H1", "High Intent, No Action"
        ENGAGING_NOW_SILENT = "H2", "Was Engaging, Now Silent"
        NO_AUTOMATION = "H3", "No Automation Running"
        FOLLOWUPS_EXHAUSTED = "S1", "Follow-ups Exhausted, Lead Silent"
        SEQUENCE_COMPLETE_NO_MOVE = "S2", "Sequence Complete, No Stage Move"
        NO_PHONE = "X2", "No Phone Number"
        STAGE_STALE = "X3", "Stage Stale"
        LONG_TERM_DORMANT = "X4", "Long-Term Dormant"

    class Severity(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"
        CRITICAL = "critical", "Critical"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="copilot_flags",
    )
    lead = models.ForeignKey(
        "crm.Lead",
        on_delete=models.CASCADE,
        related_name="copilot_flags",
    )
    flag_code = models.CharField(
        max_length=2,
        choices=FlagCode.choices,
    )
    severity = models.CharField(
        max_length=10,
        choices=Severity.choices,
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
    )
    first_detected_at = models.DateTimeField(auto_now_add=True)
    last_detected_at = models.DateTimeField(auto_now=True)
    snoozed_until = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_detected_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "lead", "flag_code"],
                name="uniq_copilot_org_lead_flag",
            )
        ]
        indexes = [
            models.Index(
                fields=["organization", "severity"],
                name="copilot_org_severity_idx",
            ),
            models.Index(
                fields=["organization", "flag_code"],
                name="copilot_org_code_idx",
            ),
            models.Index(
                fields=["organization", "snoozed_until"],
                name="copilot_org_snooze_idx",
            ),
        ]

    def __str__(self):
        return f"{self.flag_code} · {self.lead_id} · {self.severity}"


class CopilotScanState(models.Model):
    """Tracks the last completed cache refresh for an organization."""

    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="copilot_scan_state",
        primary_key=True,
    )
    last_refreshed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Co-Pilot scan · {self.organization_id}"
