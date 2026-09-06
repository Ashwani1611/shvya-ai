import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class AutoFollowupSettings(models.Model):
    """Organization-level execution controls for Auto Follow-ups."""

    class DelayUnit(models.TextChoices):
        MINUTES = "minutes", "Minutes"
        HOURS = "hours", "Hours"
        DAYS = "days", "Days"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="auto_followup_settings",
    )
    enabled = models.BooleanField(default=False)
    business_hours_start = models.TimeField(default="07:30")
    business_hours_end = models.TimeField(default="19:30")
    conversation_delay_value = models.PositiveIntegerField(default=2)
    conversation_delay_unit = models.CharField(
        max_length=10,
        choices=DelayUnit.choices,
        default=DelayUnit.HOURS,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Auto Follow-ups — {self.organization.name}"


class FollowupSequence(models.Model):
    """Reusable, ordered set of WhatsApp/email/reminder steps."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="followup_sequences",
    )
    name = models.CharField(max_length=255)
    description = models.CharField(max_length=300, blank=True)
    whatsapp_account = models.ForeignKey(
        "channels.WhatsAppAccount",
        on_delete=models.PROTECT,
        related_name="followup_sequences",
    )
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_followup_sequences",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"],
                name="fu_seq_org_name_uniq",
            )
        ]
        indexes = [
            models.Index(
                fields=["organization", "is_active", "updated_at"],
                name="fu_seq_org_active_idx",
            )
        ]

    def __str__(self):
        return self.name


class FollowupStep(models.Model):
    """One ordered sequence action and its schedule configuration."""

    class StepType(models.TextChoices):
        WHATSAPP = "whatsapp", "WhatsApp API"
        EMAIL = "email", "Email"
        REMINDER = "reminder", "Call Reminder"

    class ScheduleType(models.TextChoices):
        IMMEDIATE = "immediate", "Immediately"
        DELAY = "delay", "After X minutes/hours/days"
        SPECIFIC_TIME = "specific_time", "Specific time"
        RECURRING = "recurring", "Recurring"

    class DelayUnit(models.TextChoices):
        MINUTES = "minutes", "Minutes"
        HOURS = "hours", "Hours"
        DAYS = "days", "Days"

    class Weekday(models.IntegerChoices):
        SUNDAY = 0, "Sunday"
        MONDAY = 1, "Monday"
        TUESDAY = 2, "Tuesday"
        WEDNESDAY = 3, "Wednesday"
        THURSDAY = 4, "Thursday"
        FRIDAY = 5, "Friday"
        SATURDAY = 6, "Saturday"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sequence = models.ForeignKey(
        FollowupSequence,
        on_delete=models.CASCADE,
        related_name="steps",
    )
    position = models.PositiveIntegerField(default=1)
    step_type = models.CharField(max_length=12, choices=StepType.choices)
    title = models.CharField(max_length=255, blank=True)

    whatsapp_template = models.ForeignKey(
        "channels.WhatsAppTemplate",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="followup_steps",
    )
    email_subject = models.CharField(max_length=255, blank=True)
    email_body = models.TextField(blank=True)
    reminder_text = models.TextField(blank=True)

    schedule_type = models.CharField(
        max_length=16,
        choices=ScheduleType.choices,
        default=ScheduleType.IMMEDIATE,
    )
    delay_value = models.PositiveIntegerField(null=True, blank=True)
    delay_unit = models.CharField(
        max_length=10,
        choices=DelayUnit.choices,
        blank=True,
    )
    specific_time = models.TimeField(null=True, blank=True)
    specific_weekday = models.SmallIntegerField(
        choices=Weekday.choices,
        null=True,
        blank=True,
    )
    recurring_every = models.PositiveIntegerField(null=True, blank=True)
    recurring_unit = models.CharField(
        max_length=10,
        choices=DelayUnit.choices,
        blank=True,
    )
    recurring_weekdays = models.JSONField(default=list, blank=True)

    retry_count = models.PositiveSmallIntegerField(default=0)
    retry_delay_hours = models.PositiveSmallIntegerField(default=24)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["sequence", "position"],
                name="fu_step_seq_position_uniq",
            ),
            models.CheckConstraint(
                condition=Q(retry_count__gte=0, retry_count__lte=5),
                name="fu_step_retry_lte_5",
            ),
        ]
        indexes = [
            models.Index(
                fields=["sequence", "position", "is_active"],
                name="fu_step_seq_order_idx",
            )
        ]

    def __str__(self):
        return f"{self.sequence.name} · {self.position} · {self.get_step_type_display()}"


class LeadSequenceState(models.Model):
    """
    First-class lead-to-sequence progress.

    Rows are retained when a lead switches sequences so returning to an older
    sequence can resume at the last completed step instead of starting over.
    Exactly one sequence may be active/paused for a lead at a time.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        INACTIVE = "inactive", "Inactive"
        COMPLETED = "completed", "Completed"
        CLEARED = "cleared", "Cleared"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="lead_sequence_states",
    )
    lead = models.ForeignKey(
        "crm.Lead",
        on_delete=models.CASCADE,
        related_name="followup_sequence_states",
    )
    sequence = models.ForeignKey(
        FollowupSequence,
        on_delete=models.CASCADE,
        related_name="lead_states",
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_followup_sequence_states",
    )
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    lead_auto_followup_enabled = models.BooleanField(default=True)
    next_step = models.ForeignKey(
        FollowupStep,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="next_for_states",
    )
    last_completed_position = models.PositiveIntegerField(default=0)
    upcoming_send_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    last_step_completed_at = models.DateTimeField(null=True, blank=True)
    last_inbound_at = models.DateTimeField(null=True, blank=True)
    last_manual_outbound_at = models.DateTimeField(null=True, blank=True)
    paused_until = models.DateTimeField(null=True, blank=True)
    assigned_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    cleared_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["upcoming_send_at", "assigned_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["lead", "sequence"],
                name="fu_state_lead_seq_uniq",
            ),
            models.UniqueConstraint(
                fields=["lead"],
                condition=Q(status__in=["active", "paused"]),
                name="fu_state_one_active_lead",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "status", "upcoming_send_at"],
                name="fu_state_org_due_idx",
            ),
            models.Index(
                fields=["sequence", "status"],
                name="fu_state_seq_status_idx",
            ),
        ]

    def __str__(self):
        return f"{self.lead.name} → {self.sequence.name} ({self.status})"


class FollowupExecution(models.Model):
    """Immutable-ish schedule/attempt history for every sequence step."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        SENT = "sent", "Sent"
        CREATED = "created", "Reminder created"
        RETRY_WAIT = "retry_wait", "Retry wait"
        SKIPPED = "skipped", "Skipped"
        BLOCKED = "blocked", "Blocked"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="followup_executions",
    )
    state = models.ForeignKey(
        LeadSequenceState,
        on_delete=models.CASCADE,
        related_name="executions",
    )
    lead = models.ForeignKey(
        "crm.Lead",
        on_delete=models.CASCADE,
        related_name="followup_executions",
    )
    sequence = models.ForeignKey(
        FollowupSequence,
        on_delete=models.CASCADE,
        related_name="executions",
    )
    step = models.ForeignKey(
        FollowupStep,
        on_delete=models.PROTECT,
        related_name="executions",
    )
    scheduled_for = models.DateTimeField(db_index=True)
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.PENDING,
    )
    attempt_no = models.PositiveSmallIntegerField(default=1)
    max_attempts = models.PositiveSmallIntegerField(default=1)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    whatsapp_message = models.ForeignKey(
        "channels.WhatsAppMessage",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="followup_executions",
    )
    reminder = models.ForeignKey(
        "crm.LeadReminder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="followup_executions",
    )
    email_message_id = models.CharField(max_length=255, blank=True)
    error = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["state", "step", "status"],
                name="fu_exec_state_step_idx",
            ),
            models.Index(
                fields=["organization", "status", "scheduled_for"],
                name="fu_exec_org_due_idx",
            ),
        ]

    def __str__(self):
        return f"{self.lead.name} · {self.step_id} · {self.status}"


class FollowupSenderState(models.Model):
    """Per WhatsApp sender throttle so due leads are drained sequentially."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.OneToOneField(
        "channels.WhatsAppAccount",
        on_delete=models.CASCADE,
        related_name="followup_sender_state",
    )
    next_available_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    last_lead = models.ForeignKey(
        "crm.Lead",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="last_followup_sender_states",
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Follow-up sender — {self.account}"
