import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class CallIntelligenceSettings(models.Model):
    """Organization-level policy for Android Call Intelligence."""

    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="call_intelligence_settings",
    )
    is_enabled = models.BooleanField(default=True)
    auto_create_answered = models.BooleanField(default=True)
    auto_create_missed = models.BooleanField(default=True)
    auto_create_outbound = models.BooleanField(default=True)
    default_country_code = models.CharField(max_length=8, default="+91")
    default_pipeline = models.ForeignKey(
        "crm.Pipeline",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    default_stage = models.ForeignKey(
        "crm.Stage",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        super().clean()
        if self.default_pipeline_id and (
            self.default_pipeline.organization_id != self.organization_id
        ):
            raise ValidationError(
                {"default_pipeline": "Pipeline must belong to this organization."}
            )
        if self.default_stage_id:
            if not self.default_pipeline_id:
                raise ValidationError(
                    {"default_stage": "Select a default pipeline first."}
                )
            if self.default_stage.pipeline_id != self.default_pipeline_id:
                raise ValidationError(
                    {"default_stage": "Stage must belong to the default pipeline."}
                )

    def __str__(self):
        return f"Call Intelligence · {self.organization}"


class CallDevice(models.Model):
    """An authenticated Android device allowed to publish SIM call events."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="call_intelligence_devices",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="call_intelligence_devices",
    )
    device_uuid = models.UUIDField()
    device_name = models.CharField(max_length=150, blank=True)
    manufacturer = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=100, blank=True)
    android_version = models.CharField(max_length=40, blank=True)
    app_version = models.CharField(max_length=40, blank=True)
    permissions = models.JSONField(default=dict, blank=True)
    battery_optimization_ignored = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    registered_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_seen_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "device_uuid"],
                name="uniq_call_device_org_uuid",
            )
        ]
        indexes = [
            models.Index(
                fields=["organization", "user", "-last_seen_at"],
                name="calls_device_org_user_seen_idx",
            )
        ]

    def clean(self):
        super().clean()
        if self.user_id and self.user.organization_id != self.organization_id:
            raise ValidationError({"user": "Device user must belong to this organization."})

    def __str__(self):
        return self.device_name or str(self.device_uuid)


class CallRecord(models.Model):
    """Server-authoritative call record produced from Android SIM events."""

    class Source(models.TextChoices):
        ANDROID_SIM = "android_sim", "Android SIM"
        MANUAL = "manual", "Manual"
        PROVIDER = "provider", "Provider"

    class Direction(models.TextChoices):
        INBOUND = "inbound", "Incoming"
        OUTBOUND = "outbound", "Outgoing"
        UNKNOWN = "unknown", "Unknown"

    class Status(models.TextChoices):
        INITIATED = "initiated", "Initiated"
        RINGING = "ringing", "Ringing"
        ANSWERED = "answered", "Answered"
        COMPLETED = "completed", "Completed"
        MISSED = "missed", "Missed"
        REJECTED = "rejected", "Rejected"
        BUSY = "busy", "Busy"
        NO_ANSWER = "no_answer", "No Answer"
        FAILED = "failed", "Failed"
        UNKNOWN = "unknown", "Unknown"

    class LeadMatchStatus(models.TextChoices):
        MATCHED = "matched", "Matched"
        AUTO_CREATED = "auto_created", "Auto-created"
        UNMATCHED = "unmatched", "Unmatched"
        RESTRICTED = "restricted", "Matched outside user access"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="call_intelligence_calls",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="call_intelligence_calls",
    )
    device = models.ForeignKey(
        CallDevice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="calls",
    )
    lead = models.ForeignKey(
        "crm.Lead",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="intelligence_calls",
    )
    crm_call = models.OneToOneField(
        "crm.LeadCall",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="intelligence_record",
    )
    pipeline = models.ForeignKey(
        "crm.Pipeline",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    stage = models.ForeignKey(
        "crm.Stage",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    source = models.CharField(
        max_length=30,
        choices=Source.choices,
        default=Source.ANDROID_SIM,
    )
    source_call_id = models.CharField(max_length=160, blank=True)
    direction = models.CharField(
        max_length=20,
        choices=Direction.choices,
        default=Direction.UNKNOWN,
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.UNKNOWN,
    )
    lead_match_status = models.CharField(
        max_length=20,
        choices=LeadMatchStatus.choices,
        default=LeadMatchStatus.UNMATCHED,
    )

    phone_number = models.CharField(max_length=32)
    raw_phone_number = models.CharField(max_length=64, blank=True)
    contact_name = models.CharField(max_length=180, blank=True)
    sim_slot = models.CharField(max_length=30, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    ringing_at = models.DateTimeField(null=True, blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    called_at = models.DateTimeField()
    ring_duration_seconds = models.PositiveIntegerField(default=0)
    duration_seconds = models.PositiveIntegerField(default=0)

    notes = models.TextField(blank=True)
    disposition = models.CharField(max_length=80, blank=True)
    follow_up_required = models.BooleanField(default=False)
    follow_up_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    first_received_at = models.DateTimeField(auto_now_add=True)
    last_received_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-called_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source", "source_call_id"],
                condition=~Q(source_call_id=""),
                name="uniq_call_org_source_id",
            )
        ]
        indexes = [
            models.Index(
                fields=["organization", "-called_at"],
                name="calls_org_called_idx",
            ),
            models.Index(
                fields=["organization", "phone_number", "-called_at"],
                name="calls_org_phone_called_idx",
            ),
            models.Index(
                fields=["user", "-called_at"],
                name="calls_user_called_idx",
            ),
            models.Index(
                fields=["lead", "-called_at"],
                name="calls_lead_called_idx",
            ),
            models.Index(
                fields=["organization", "status", "-called_at"],
                name="calls_org_status_called_idx",
            ),
        ]

    def __str__(self):
        return f"{self.get_direction_display()} · {self.phone_number}"


class CallEvent(models.Model):
    """Immutable event evidence received from an Android outbox."""

    class Type(models.TextChoices):
        DETECTED = "detected", "Detected"
        RINGING = "ringing", "Ringing"
        OFFHOOK = "offhook", "Off hook"
        ANSWERED = "answered", "Answered"
        IDLE = "idle", "Idle"
        COMPLETED = "completed", "Completed"
        MISSED = "missed", "Missed"
        REJECTED = "rejected", "Rejected"
        RECONCILED = "reconciled", "Call log reconciled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    call = models.ForeignKey(
        CallRecord,
        on_delete=models.CASCADE,
        related_name="events",
    )
    device = models.ForeignKey(
        CallDevice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )
    event_uuid = models.UUIDField(unique=True)
    event_type = models.CharField(max_length=30, choices=Type.choices)
    occurred_at = models.DateTimeField()
    payload = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["occurred_at", "received_at"]
        indexes = [
            models.Index(
                fields=["call", "occurred_at"],
                name="calls_event_call_time_idx",
            )
        ]

    def __str__(self):
        return f"{self.call_id} · {self.event_type}"


class CallIntelligence(models.Model):
    """AI-ready enrichment kept separate from immutable call evidence."""

    class AnalysisStatus(models.TextChoices):
        NOT_AVAILABLE = "not_available", "Not available"
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    class Intent(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    call = models.OneToOneField(
        CallRecord,
        on_delete=models.CASCADE,
        related_name="intelligence",
    )
    analysis_status = models.CharField(
        max_length=20,
        choices=AnalysisStatus.choices,
        default=AnalysisStatus.NOT_AVAILABLE,
    )
    recording_url = models.URLField(max_length=1000, blank=True)
    transcript = models.TextField(blank=True)
    summary = models.TextField(blank=True)
    intent = models.CharField(
        max_length=20,
        choices=Intent.choices,
        default=Intent.UNKNOWN,
    )
    sentiment = models.CharField(max_length=40, blank=True)
    outcome = models.CharField(max_length=100, blank=True)
    objections = models.JSONField(default=list, blank=True)
    buying_signals = models.JSONField(default=list, blank=True)
    competitors = models.JSONField(default=list, blank=True)
    extracted_attributes = models.JSONField(default=dict, blank=True)
    next_action = models.CharField(max_length=255, blank=True)
    follow_up_at = models.DateTimeField(null=True, blank=True)
    ai_score = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        null=True,
        blank=True,
    )
    talk_ratio = models.JSONField(default=dict, blank=True)
    analysis_payload = models.JSONField(default=dict, blank=True)
    analyzed_at = models.DateTimeField(null=True, blank=True)
    analysis_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Intelligence · {self.call_id}"
