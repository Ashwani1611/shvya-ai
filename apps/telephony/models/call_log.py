import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.organizations.models import Organization


class CallIntelligenceSettings(models.Model):
    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="call_intelligence_settings",
    )
    enabled = models.BooleanField(default=True)
    auto_create_answered_incoming = models.BooleanField(default=True)
    auto_create_answered_outgoing = models.BooleanField(default=True)
    auto_create_missed = models.BooleanField(default=True)
    auto_create_rejected = models.BooleanField(default=False)
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
    default_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        super().clean()
        if self.default_pipeline_id and (
            self.default_pipeline.organization_id != self.organization_id
        ):
            raise ValidationError(
                {"default_pipeline": "Pipeline must belong to this organization."}
            )
        if self.default_stage_id and (
            not self.default_pipeline_id
            or self.default_stage.pipeline_id != self.default_pipeline_id
        ):
            raise ValidationError(
                {"default_stage": "Stage must belong to the selected pipeline."}
            )
        if self.default_owner_id and (
            self.default_owner.organization_id != self.organization_id
        ):
            raise ValidationError(
                {"default_owner": "Owner must belong to this organization."}
            )

    def __str__(self):
        return f"{self.organization.name} — Call Intelligence"


class CallDevice(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="call_intelligence_devices",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="call_intelligence_devices",
    )
    device_id = models.CharField(max_length=128, unique=True)
    name = models.CharField(max_length=150, blank=True)
    manufacturer = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=100, blank=True)
    android_version = models.CharField(max_length=50, blank=True)
    app_version = models.CharField(max_length=50, blank=True)
    permissions = models.JSONField(default=dict, blank=True)
    battery_optimization_ignored = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["organization", "user", "is_active"],
                name="telephony_device_user_idx",
            )
        ]

    def clean(self):
        super().clean()
        if self.user_id and self.organization_id:
            if self.user.organization_id != self.organization_id:
                raise ValidationError("Device user must belong to its organization.")

    def __str__(self):
        return self.name or f"{self.user} — {self.device_id}"


class CallRecord(models.Model):
    class Source(models.TextChoices):
        ANDROID_SIM = "android_sim", "Android SIM"
        CLOUD = "cloud", "Cloud telephony"
        MANUAL = "manual", "Manual"

    class Direction(models.TextChoices):
        INCOMING = "incoming", "Incoming"
        OUTGOING = "outgoing", "Outgoing"

    class Status(models.TextChoices):
        ANSWERED = "answered", "Answered"
        MISSED = "missed", "Missed"
        REJECTED = "rejected", "Rejected"
        BUSY = "busy", "Busy"
        NO_ANSWER = "no_answer", "No Answer"
        FAILED = "failed", "Failed"
        UNKNOWN = "unknown", "Unknown"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="call_intelligence_calls",
    )
    device = models.ForeignKey(
        CallDevice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="calls",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="call_intelligence_calls",
    )
    lead = models.ForeignKey(
        "crm.Lead",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="intelligent_calls",
    )
    crm_call = models.OneToOneField(
        "crm.LeadCall",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="intelligence_record",
    )
    source = models.CharField(max_length=24, choices=Source.choices, default=Source.ANDROID_SIM)
    source_call_id = models.CharField(max_length=255)
    phone_number = models.CharField(max_length=32)
    raw_phone_number = models.CharField(max_length=64, blank=True)
    contact_name = models.CharField(max_length=255, blank=True)
    direction = models.CharField(max_length=16, choices=Direction.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UNKNOWN)
    sub_status = models.CharField(max_length=40, blank=True)
    call_name = models.CharField(max_length=150, default="Phone Call")
    started_at = models.DateTimeField(null=True, blank=True)
    ringing_at = models.DateTimeField(null=True, blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    ring_duration_seconds = models.PositiveIntegerField(default=0)
    talk_duration_seconds = models.PositiveIntegerField(default=0)
    total_duration_seconds = models.PositiveIntegerField(default=0)
    notes = models.TextField(blank=True)
    disposition = models.CharField(max_length=80, blank=True)
    follow_up_required = models.BooleanField(default=False)
    follow_up_at = models.DateTimeField(null=True, blank=True)
    sync_status = models.CharField(
        max_length=16,
        choices=[
            ("pending", "Pending"),
            ("syncing", "Syncing"),
            ("synced", "Synced"),
            ("failed", "Failed"),
        ],
        default="synced",
    )
    retry_count = models.PositiveIntegerField(default=0)
    last_sync_attempt_at = models.DateTimeField(null=True, blank=True)
    sync_error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-ended_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "device", "source", "source_call_id"],
                name="telephony_source_call_unique",
            )
        ]
        indexes = [
            models.Index(fields=["organization", "-ended_at"], name="telephony_org_call_at_idx"),
            models.Index(fields=["lead", "-ended_at"], name="telephony_lead_call_idx"),
            models.Index(fields=["user", "-ended_at"], name="telephony_user_call_idx"),
            models.Index(fields=["organization", "status"], name="telephony_org_status_idx"),
        ]

    def __str__(self):
        return f"{self.get_direction_display()} {self.phone_number}"


class CallEvent(models.Model):
    class Type(models.TextChoices):
        STARTED = "started", "Started"
        RINGING = "ringing", "Ringing"
        ANSWERED = "answered", "Answered"
        COMPLETED = "completed", "Completed"
        MISSED = "missed", "Missed"
        REJECTED = "rejected", "Rejected"
        BUSY = "busy", "Busy"
        NO_ANSWER = "no_answer", "No Answer"
        FAILED = "failed", "Failed"
        RECONCILED = "reconciled", "Reconciled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_uuid = models.UUIDField(unique=True)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="call_intelligence_events",
    )
    call = models.ForeignKey(CallRecord, on_delete=models.CASCADE, related_name="events")
    device = models.ForeignKey(
        CallDevice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="call_intelligence_events",
    )
    event_type = models.CharField(max_length=20, choices=Type.choices)
    occurred_at = models.DateTimeField()
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["occurred_at", "created_at"]
        indexes = [
            models.Index(fields=["call", "occurred_at"], name="telephony_call_event_idx")
        ]


class CallIntelligenceResult(models.Model):
    class Intent(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    class Sentiment(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"
        NEGATIVE = "negative", "Negative"
        NEUTRAL = "neutral", "Neutral"
        POSITIVE = "positive", "Positive"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    call = models.OneToOneField(CallRecord, on_delete=models.CASCADE, related_name="intelligence")
    summary = models.TextField(blank=True)
    intent = models.CharField(max_length=16, choices=Intent.choices, default=Intent.UNKNOWN)
    sentiment = models.CharField(max_length=16, choices=Sentiment.choices, default=Sentiment.UNKNOWN)
    outcome = models.CharField(max_length=255, blank=True)
    objections = models.JSONField(default=list, blank=True)
    buying_signals = models.JSONField(default=list, blank=True)
    competitor = models.CharField(max_length=255, blank=True)
    budget = models.CharField(max_length=255, blank=True)
    timeline = models.CharField(max_length=255, blank=True)
    next_action = models.CharField(max_length=255, blank=True)
    extracted_attributes = models.JSONField(default=list, blank=True)
    raw_analysis = models.JSONField(default=dict, blank=True)
    model = models.CharField(max_length=100, blank=True)
    analyzed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class CallAppRelease(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version_name = models.CharField(max_length=40)
    version_code = models.PositiveIntegerField(unique=True)
    download_url = models.URLField(max_length=1000)
    sha256 = models.CharField(max_length=64, blank=True)
    min_android_sdk = models.PositiveIntegerField(default=23)
    release_notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=False, db_index=True)
    is_mandatory = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version_code", "-created_at"]

    def __str__(self):
        return f"Android {self.version_name} ({self.version_code})"
