"""Campaign operations extend, rather than replace, SHVYA CRM and WhatsApp records."""
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class CampaignUpload(models.Model):
    """Private, expiring upload/review state. Preview never creates CRM leads."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey("organizations.organization", on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    filename = models.CharField(max_length=255)
    headers = models.JSONField(default=list)
    rows = models.JSONField(default=list)
    reviewed_rows = models.JSONField(default=list)
    review_config = models.JSONField(default=dict)
    review_stats = models.JSONField(default=dict)
    review_digest = models.CharField(max_length=64, blank=True)
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)


class CampaignPlan(models.Model):
    """Operational extension of the existing canonical BulkMessageCampaign."""

    campaign = models.OneToOneField("channels.bulkmessagecampaign", primary_key=True, on_delete=models.CASCADE, related_name="campaign_plan")
    upload = models.OneToOneField("channels.campaignupload", null=True, blank=True, on_delete=models.SET_NULL, related_name="plan")
    template = models.ForeignKey("channels.whatsapptemplate", null=True, blank=True, on_delete=models.SET_NULL)
    template_snapshot = models.JSONField(default=dict)
    bindings = models.JSONField(default=dict)
    scheduled_for = models.DateTimeField(default=timezone.now, db_index=True)
    timezone = models.CharField(max_length=64, default="UTC")
    auto_retry = models.BooleanField(default=False)
    retry_attempts = models.PositiveSmallIntegerField(default=3)
    retry_delay_hours = models.PositiveSmallIntegerField(default=24)
    consent_at = models.DateTimeField()
    consent_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    prepared_at = models.DateTimeField(null=True, blank=True)
    prepare_cursor = models.PositiveIntegerField(default=0)
    stats = models.JSONField(default=dict)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    prepare_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class CampaignDelivery(models.Model):
    """Frozen recipient ledger; survives deletion of its CRM lead or legacy recipient."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    campaign = models.ForeignKey("channels.bulkmessagecampaign", on_delete=models.CASCADE, related_name="campaign_delivery_rows")
    recipient = models.OneToOneField("channels.bulkmessagerecipient", null=True, blank=True, on_delete=models.SET_NULL, related_name="delivery_state")
    lead = models.ForeignKey("crm.lead", null=True, blank=True, on_delete=models.SET_NULL, related_name="campaign_deliveries")
    name = models.CharField(max_length=150)
    phone = models.CharField(max_length=32)
    pipeline_key = models.UUIDField()
    stage_key = models.UUIDField()
    pipeline_label = models.CharField(max_length=150)
    stage_label = models.CharField(max_length=150)
    values = models.JSONField(default=dict)
    body = models.TextField()
    components = models.JSONField(default=list)
    state = models.CharField(max_length=16, default="pending", choices=[("pending", "Pending"), ("sending", "Sending"), ("accepted", "Accepted"), ("failed", "Failed"), ("skipped", "Skipped"), ("review", "Needs review")])
    due_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    claim_id = models.UUIDField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    accepted_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    replied_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.TextField(blank=True)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    uncertain = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["campaign", "phone"], name="campaign_phone_once")]
        indexes = [models.Index(fields=["state", "due_at"], name="campaign_delivery_due_idx"), models.Index(fields=["campaign", "state"], name="campaign_delivery_state_idx")]


class CampaignAttempt(models.Model):
    """Append-only send-attempt identity and monotonic provider evidence."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    delivery = models.ForeignKey("channels.campaigndelivery", on_delete=models.CASCADE, related_name="attempts")
    number = models.PositiveSmallIntegerField()
    message = models.OneToOneField("channels.whatsappmessage", null=True, blank=True, on_delete=models.SET_NULL, related_name="campaign_attempt")
    provider_id = models.CharField(max_length=128, null=True, blank=True, unique=True)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.TextField(blank=True)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    uncertain = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["delivery", "number"], name="campaign_attempt_once")]
        ordering = ["number"]


class CampaignEvent(models.Model):
    """Deduplicated durable inbox for verified provider events, including early callbacks."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    digest = models.CharField(max_length=64, unique=True)
    kind = models.CharField(max_length=16)
    account = models.ForeignKey("channels.whatsappaccount", on_delete=models.CASCADE)
    external_id = models.CharField(max_length=128, blank=True, db_index=True)
    message = models.ForeignKey("channels.whatsappmessage", null=True, blank=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=16, blank=True)
    recipient_phone = models.CharField(max_length=32, blank=True)
    occurred_at = models.DateTimeField(null=True, blank=True)
    data = models.JSONField(default=dict)
    applied_at = models.DateTimeField(null=True, blank=True, db_index=True)
    received_at = models.DateTimeField(default=timezone.now)


class CampaignSuppression(models.Model):
    """Organization-level campaign opt-out; never inferred from AI being disabled."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey("organizations.organization", on_delete=models.CASCADE)
    phone = models.CharField(max_length=32)
    reason = models.CharField(max_length=240)
    source_message = models.ForeignKey("channels.whatsappmessage", null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "phone"], name="campaign_optout_org_phone")]


class CampaignSenderGate(models.Model):
    """A database-backed send slot shared by campaigns using the same account."""

    account = models.OneToOneField("channels.whatsappaccount", on_delete=models.CASCADE, primary_key=True)
    next_slot_at = models.DateTimeField(default=timezone.now)
