import secrets
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.core.crypto import credential_cipher


def default_availability():
    return {
        "mon": {"enabled": True, "start": "09:00", "end": "18:00"},
        "tue": {"enabled": True, "start": "09:00", "end": "18:00"},
        "wed": {"enabled": True, "start": "09:00", "end": "18:00"},
        "thu": {"enabled": True, "start": "09:00", "end": "18:00"},
        "fri": {"enabled": True, "start": "09:00", "end": "18:00"},
        "sat": {"enabled": False, "start": "09:00", "end": "18:00"},
        "sun": {"enabled": False, "start": "09:00", "end": "18:00"},
    }


class CalendarPage(models.Model):
    class PageType(models.TextChoices):
        LEAD = "lead", "Lead capture"
        BOOKING = "booking", "Booking"
        LEAD_BOOKING = "lead_booking", "Lead capture + booking"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        DISABLED = "disabled", "Disabled"

    class DuplicateBehavior(models.TextChoices):
        USE_EXISTING = "use_existing", "Use existing lead"
        BLOCK = "block", "Block duplicate submission"

    class AttributeUpdatePolicy(models.TextChoices):
        FILL_BLANK = "fill_blank", "Fill blank CRM values only"
        OVERWRITE = "overwrite", "Replace mapped CRM values"
        SUBMISSION_ONLY = "submission_only", "Keep CRM values unchanged"

    class MeetingLocation(models.TextChoices):
        GOOGLE_MEET = "google_meet", "Google Meet"
        PHONE = "phone", "Phone call"
        IN_PERSON = "in_person", "In person"
        CUSTOM = "custom", "Custom meeting link"
        NONE = "none", "No meeting location"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="calendar_pages",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_calendar_pages",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_calendar_pages",
    )
    host = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="hosted_calendar_pages",
    )
    pipeline = models.ForeignKey(
        "crm.Pipeline",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="calendar_pages",
    )
    stage = models.ForeignKey(
        "crm.Stage",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="calendar_pages",
    )

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120)
    page_type = models.CharField(
        max_length=20,
        choices=PageType.choices,
        default=PageType.LEAD_BOOKING,
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    timezone = models.CharField(max_length=64, default="Asia/Kolkata")
    accent_color = models.CharField(max_length=16, default="#0060A2")
    logo_url = models.URLField(blank=True)

    intro_title = models.CharField(max_length=120, blank=True)
    intro_description = models.TextField(blank=True)
    intro_highlights = models.JSONField(default=list, blank=True)
    form_schema = models.JSONField(default=list, blank=True)
    submit_button_text = models.CharField(
        max_length=80,
        default="Continue to scheduling",
    )
    consent_enabled = models.BooleanField(default=True)
    consent_text = models.CharField(
        max_length=300,
        default="I agree to be contacted regarding this booking.",
    )

    duplicate_behavior = models.CharField(
        max_length=20,
        choices=DuplicateBehavior.choices,
        default=DuplicateBehavior.USE_EXISTING,
    )
    duplicate_match_email = models.BooleanField(default=True)
    attribute_update_policy = models.CharField(
        max_length=24,
        choices=AttributeUpdatePolicy.choices,
        default=AttributeUpdatePolicy.FILL_BLANK,
    )
    notify_host_on_submission = models.BooleanField(default=True)
    notify_user_ids = models.JSONField(default=list, blank=True)

    session_title = models.CharField(
        max_length=80,
        default="30-Minute Consultation Call",
    )
    session_description = models.CharField(max_length=250, blank=True)
    discussion_points = models.JSONField(default=list, blank=True)
    availability = models.JSONField(default=default_availability)
    bookable_days = models.PositiveSmallIntegerField(default=30)
    minimum_notice_minutes = models.PositiveIntegerField(default=60)
    slot_duration_minutes = models.PositiveSmallIntegerField(default=30)
    max_slots_per_day = models.PositiveSmallIntegerField(default=25)
    bookings_per_slot = models.PositiveSmallIntegerField(default=1)
    buffer_before_minutes = models.PositiveSmallIntegerField(default=0)
    buffer_after_minutes = models.PositiveSmallIntegerField(default=0)

    meeting_location = models.CharField(
        max_length=20,
        choices=MeetingLocation.choices,
        default=MeetingLocation.GOOGLE_MEET,
    )
    custom_meeting_link = models.URLField(blank=True)
    invite_lead_to_event = models.BooleanField(default=True)

    confirmation_heading = models.CharField(
        max_length=80,
        default="Your call has been successfully scheduled!",
    )
    confirmation_message = models.CharField(
        max_length=250,
        default=(
            "Your slot is confirmed. We've saved your details and our team "
            "will connect with you at the scheduled time."
        ),
    )
    show_booking_details = models.BooleanField(default=True)
    show_add_calendar = models.BooleanField(default=True)
    redirect_enabled = models.BooleanField(default=False)
    redirect_button_text = models.CharField(max_length=80, blank=True)
    redirect_url = models.URLField(blank=True)

    current_version = models.PositiveIntegerField(default=0)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "slug"],
                name="uniq_calendar_page_org_slug",
            )
        ]
        indexes = [
            models.Index(
                fields=["organization", "status"],
                name="cal_page_org_status_idx",
            )
        ]

    def clean(self):
        super().clean()
        if self.pipeline_id and self.pipeline.organization_id != self.organization_id:
            raise ValidationError({"pipeline": "Pipeline must belong to this organization."})
        if self.stage_id:
            if not self.pipeline_id or self.stage.pipeline_id != self.pipeline_id:
                raise ValidationError({"stage": "Stage must belong to the selected pipeline."})
        if self.host_id and self.host.organization_id != self.organization_id:
            raise ValidationError({"host": "Host must belong to this organization."})
        if self.meeting_location == self.MeetingLocation.CUSTOM and not self.custom_meeting_link:
            raise ValidationError(
                {"custom_meeting_link": "A custom meeting link is required."}
            )
        if self.bookings_per_slot < 1:
            raise ValidationError({"bookings_per_slot": "Must be at least 1."})
        if self.slot_duration_minutes < 5:
            raise ValidationError({"slot_duration_minutes": "Must be at least 5 minutes."})

    def __str__(self):
        return f"{self.organization.name} · {self.name}"


class CalendarPageVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    page = models.ForeignKey(
        CalendarPage,
        on_delete=models.CASCADE,
        related_name="published_versions",
    )
    version = models.PositiveIntegerField()
    snapshot = models.JSONField(default=dict)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="calendar_page_publications",
    )
    published_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["page", "version"],
                name="uniq_calendar_page_version",
            )
        ]

    def __str__(self):
        return f"{self.page.name} v{self.version}"


class CalendarSubmission(models.Model):
    class Status(models.TextChoices):
        LEAD_CREATED = "lead_created", "Lead created"
        LEAD_MATCHED = "lead_matched", "Existing lead matched"
        DUPLICATE_BLOCKED = "duplicate_blocked", "Duplicate blocked"
        INVALID = "invalid", "Invalid"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="calendar_submissions",
    )
    page = models.ForeignKey(
        CalendarPage,
        on_delete=models.CASCADE,
        related_name="submissions",
    )
    page_version = models.ForeignKey(
        CalendarPageVersion,
        on_delete=models.PROTECT,
        related_name="submissions",
    )
    lead = models.ForeignKey(
        "crm.Lead",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="calendar_submissions",
    )
    status = models.CharField(max_length=24, choices=Status.choices)
    submitted_data = models.JSONField(default=dict)
    normalized_data = models.JSONField(default=dict)
    attribution = models.JSONField(default=dict, blank=True)
    consent_accepted = models.BooleanField(default=False)
    consent_text = models.TextField(blank=True)
    consent_version = models.PositiveIntegerField(default=0)
    referrer = models.URLField(blank=True)
    user_agent = models.CharField(max_length=500, blank=True)
    ip_hash = models.CharField(max_length=64, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["organization", "page", "created_at"],
                name="cal_sub_org_page_at_idx",
            )
        ]

    def __str__(self):
        return f"{self.page.name} · {self.created_at:%Y-%m-%d %H:%M}"


class CalendarSubmissionAttachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    submission = models.ForeignKey(
        CalendarSubmission,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    field_key = models.CharField(max_length=80)
    file = models.FileField(upload_to="calendar/submissions/%Y/%m/")
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)


class CalendarBlock(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    page = models.ForeignKey(
        CalendarPage,
        on_delete=models.CASCADE,
        related_name="blocks",
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    reason = models.CharField(max_length=160, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["starts_at"]
        indexes = [
            models.Index(
                fields=["page", "starts_at", "ends_at"],
                name="cal_block_page_range_idx",
            )
        ]

    def clean(self):
        if self.starts_at >= self.ends_at:
            raise ValidationError({"ends_at": "End must be after start."})


class GoogleCalendarConnection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="google_calendar_connections",
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="google_calendar_connection",
    )
    email = models.EmailField(blank=True)
    calendar_id = models.CharField(max_length=255, default="primary")
    access_token_ciphertext = models.TextField(blank=True)
    refresh_token_ciphertext = models.TextField(blank=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    scope = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    last_error = models.TextField(blank=True)
    connected_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        super().clean()
        if self.user_id and self.user.organization_id != self.organization_id:
            raise ValidationError({"user": "Google Calendar user must be in this organization."})

    @staticmethod
    def _cipher():
        return credential_cipher(purpose="shvya-calendar-v1")

    @property
    def access_token(self):
        if not self.access_token_ciphertext:
            return ""
        return self._cipher().decrypt(
            self.access_token_ciphertext.encode("utf-8")
        ).decode("utf-8")

    @access_token.setter
    def access_token(self, value):
        self.access_token_ciphertext = (
            self._cipher().encrypt(str(value).encode("utf-8")).decode("utf-8")
            if value
            else ""
        )

    @property
    def refresh_token(self):
        if not self.refresh_token_ciphertext:
            return ""
        return self._cipher().decrypt(
            self.refresh_token_ciphertext.encode("utf-8")
        ).decode("utf-8")

    @refresh_token.setter
    def refresh_token(self, value):
        self.refresh_token_ciphertext = (
            self._cipher().encrypt(str(value).encode("utf-8")).decode("utf-8")
            if value
            else ""
        )

    def __str__(self):
        return self.email or self.user.email


class CalendarBooking(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        RESCHEDULED = "rescheduled", "Rescheduled"
        CANCELLED = "cancelled", "Cancelled"
        COMPLETED = "completed", "Completed"
        NO_SHOW = "no_show", "No-show"

    class SyncStatus(models.TextChoices):
        NOT_CONNECTED = "not_connected", "Not connected"
        PENDING = "pending", "Pending"
        SYNCED = "synced", "Synced"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="calendar_bookings",
    )
    page = models.ForeignKey(
        CalendarPage,
        on_delete=models.CASCADE,
        related_name="bookings",
    )
    submission = models.ForeignKey(
        CalendarSubmission,
        on_delete=models.PROTECT,
        related_name="bookings",
    )
    lead = models.ForeignKey(
        "crm.Lead",
        on_delete=models.PROTECT,
        related_name="calendar_bookings",
    )
    host = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="calendar_bookings",
    )
    start_at = models.DateTimeField(db_index=True)
    end_at = models.DateTimeField()
    timezone = models.CharField(max_length=64)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.SCHEDULED,
        db_index=True,
    )
    meeting_link = models.URLField(blank=True)
    google_calendar_id = models.CharField(max_length=255, blank=True)
    google_event_id = models.CharField(max_length=255, blank=True)
    google_conference_id = models.CharField(max_length=255, blank=True)
    google_event_url = models.URLField(blank=True)
    calendar_sync_status = models.CharField(
        max_length=20,
        choices=SyncStatus.choices,
        default=SyncStatus.NOT_CONNECTED,
    )
    calendar_sync_error = models.TextField(blank=True)
    cancel_token = models.CharField(max_length=64, unique=True, editable=False)
    reschedule_token = models.CharField(max_length=64, unique=True, editable=False)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    previous_start_at = models.DateTimeField(null=True, blank=True)
    previous_end_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_at"]
        indexes = [
            models.Index(
                fields=["page", "start_at", "status"],
                name="cal_book_page_slot_idx",
            ),
            models.Index(
                fields=["organization", "start_at"],
                name="cal_book_org_start_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self.cancel_token:
            self.cancel_token = secrets.token_urlsafe(32)
        if not self.reschedule_token:
            self.reschedule_token = secrets.token_urlsafe(32)
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        if self.start_at >= self.end_at:
            raise ValidationError({"end_at": "Booking end must be after start."})
        if self.page_id and self.page.organization_id != self.organization_id:
            raise ValidationError({"page": "Page belongs to another organization."})
        if self.lead_id and self.lead.organization_id != self.organization_id:
            raise ValidationError({"lead": "Lead belongs to another organization."})

    def __str__(self):
        return f"{self.lead.name} · {self.start_at:%Y-%m-%d %H:%M}"


class CalendarReminderSequence(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    page = models.OneToOneField(
        CalendarPage,
        on_delete=models.CASCADE,
        related_name="reminder_sequence",
    )
    name = models.CharField(max_length=255, default="Calendar Reminders")
    description = models.CharField(
        max_length=300,
        default="Reminder messages for calendar bookings",
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class CalendarReminderStep(models.Model):
    class Channel(models.TextChoices):
        WHATSAPP = "whatsapp", "WhatsApp"
        EMAIL = "email", "Email"
        CALL_REMINDER = "call_reminder", "Call Reminder"

    class TimingMode(models.TextChoices):
        IMMEDIATE = "immediate", "Immediately after booking"
        BEFORE = "before", "Before booked slot"
        SPECIFIC_TIME = "specific_time", "Specific time on booking date"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sequence = models.ForeignKey(
        CalendarReminderSequence,
        on_delete=models.CASCADE,
        related_name="steps",
    )
    channel = models.CharField(max_length=20, choices=Channel.choices)
    name = models.CharField(max_length=255)
    subject = models.CharField(max_length=180, blank=True)
    body = models.TextField(blank=True)
    timing_mode = models.CharField(
        max_length=20,
        choices=TimingMode.choices,
        default=TimingMode.BEFORE,
    )
    offset_minutes = models.IntegerField(
        default=-120,
        help_text="Minutes relative to booking start for before-slot reminders.",
    )
    specific_time = models.TimeField(null=True, blank=True)
    display_order = models.PositiveSmallIntegerField(default=0)
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["sequence", "display_order"],
                name="uniq_calendar_reminder_step_order",
            )
        ]


class CalendarReminderDelivery(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        ATTENTION = "attention", "Needs call"
        SENT = "sent", "Sent"
        COMPLETED = "completed", "Completed"
        SKIPPED = "skipped", "Skipped"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey(
        CalendarBooking,
        on_delete=models.CASCADE,
        related_name="reminder_deliveries",
    )
    step = models.ForeignKey(
        CalendarReminderStep,
        on_delete=models.PROTECT,
        related_name="deliveries",
    )
    due_at = models.DateTimeField(db_index=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    rendered_subject = models.CharField(max_length=180, blank=True)
    rendered_body = models.TextField(blank=True)
    error = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["due_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["booking", "step"],
                name="uniq_calendar_booking_reminder_step",
            )
        ]
        indexes = [
            models.Index(
                fields=["status", "due_at"],
                name="cal_rem_status_due_idx",
            )
        ]
