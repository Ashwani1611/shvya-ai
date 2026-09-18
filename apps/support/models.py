from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from .policy import DEFAULT_EXTENSIONS, STATES, platform_staff, valid_knowledge_url
from .storage import attachment_path, private_storage


class NamedOption(models.Model):
    name = models.CharField(max_length=100)
    active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        abstract = True
        ordering = ("order", "name")

    def __str__(self):
        return self.name


class TicketStatus(NamedOption):
    key = models.SlugField(max_length=60, unique=True)
    behavior = models.CharField(max_length=20, choices=[(s, s.replace("_", " ").title()) for s in STATES])
    system = models.BooleanField(default=False, editable=False)

    def clean(self):
        super().clean()
        original = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if original and original.system and (
            self.key != original.key or self.behavior != original.behavior or not self.active
        ):
            raise ValidationError("Built-in status behavior, key and availability cannot be changed. Rename its label instead.")


class TicketPriority(NamedOption):
    key = models.SlugField(max_length=60, unique=True)


class TicketCategory(NamedOption):
    name = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=250, blank=True)


class TicketIssue(NamedOption):
    category = models.ForeignKey(TicketCategory, on_delete=models.PROTECT, related_name="issues")
    guidance = models.CharField(max_length=350, blank=True)

    class Meta(NamedOption.Meta):
        constraints = [models.UniqueConstraint(fields=("category", "name"), name="support_unique_issue")]


class CustomField(NamedOption):
    class Kind(models.TextChoices):
        TEXT = "text", "Short text"
        LONG = "long", "Long text"
        SELECT = "select", "Dropdown"
        NUMBER = "number", "Number"
        DATE = "date", "Date"
        CHECKBOX = "checkbox", "Checkbox"

    key = models.SlugField(max_length=60, unique=True)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.TEXT)
    options = models.TextField(blank=True, help_text="Dropdown choices, one per line.")
    customer_visible = models.BooleanField(default=True)
    required = models.BooleanField(default=False, help_text="Required when the relevant user supplies this field.")
    required_on_close = models.BooleanField(default=False)
    help_text = models.CharField(max_length=200, blank=True)

    def clean(self):
        super().clean()
        if self.kind == self.Kind.SELECT and not self.choices:
            raise ValidationError("A dropdown needs at least one choice.")
        if len(self.choices) > 100:
            raise ValidationError("Use at most 100 dropdown choices.")
        old = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if old and (old.key != self.key or old.kind != self.kind):
            raise ValidationError("Field keys and types are immutable; create a new field to change them.")

    @property
    def choices(self):
        return list(dict.fromkeys(x.strip() for x in self.options.splitlines() if x.strip()))


class SupportSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    auto_assign_first_reply = models.BooleanField(default=False)
    notify_customer = models.BooleanField(default=True)
    notify_staff = models.BooleanField(default=True)
    assignee_only_notifications = models.BooleanField(default=False)
    notify_organization_admins = models.BooleanField(default=False)
    allow_customer_close = models.BooleanField(default=True)
    auto_close_hours = models.PositiveIntegerField(default=0, validators=[MaxValueValidator(8760)])
    max_files = models.PositiveIntegerField(default=8, validators=[MinValueValidator(1), MaxValueValidator(20)])
    max_file_mb = models.PositiveIntegerField(default=25, validators=[MinValueValidator(1), MaxValueValidator(100)])
    max_total_mb = models.PositiveIntegerField(default=100, validators=[MinValueValidator(1), MaxValueValidator(200)])
    allowed_extensions = models.TextField(default=DEFAULT_EXTENSIONS)
    email_intake_enabled = models.BooleanField(default=False)
    email_replies_only = models.BooleanField(default=False)
    email_issue = models.ForeignKey(TicketIssue, on_delete=models.PROTECT, null=True, blank=True,
                                    help_text="Default issue for new email tickets; its category is used automatically.")
    email_priority = models.ForeignKey(TicketPriority, on_delete=models.PROTECT, null=True, blank=True)
    blocked_senders = models.TextField(blank=True, help_text="Exact emails or @domains, one per line.")
    blocked_subject_terms = models.TextField(blank=True, help_text="Blocked phrases, one per line.")
    new_tickets_per_hour = models.PositiveIntegerField(default=20, validators=[MinValueValidator(1), MaxValueValidator(100)])

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="support_settings_singleton")]

    def clean(self):
        super().clean()
        if self.email_intake_enabled and not self.email_replies_only:
            if not self.email_issue_id or not self.email_priority_id:
                raise ValidationError("Choose the default email issue and priority before enabling new email tickets.")
            if not (self.email_issue.active and self.email_issue.category.active and self.email_priority.active):
                raise ValidationError("Email defaults must use an active category, issue and priority.")

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]

    def __str__(self):
        return "Shvya-Ops settings"


class OrganizationSupportPolicy(models.Model):
    organization = models.OneToOneField("organizations.Organization", on_delete=models.CASCADE,
                                        related_name="support_policy")
    own_tickets_only = models.BooleanField(default=False)
    email_notifications = models.BooleanField(default=True)

    def __str__(self):
        return str(self.organization)


def ticket_reference():
    return "SHV-" + uuid.uuid4().hex[:12].upper()


class Ticket(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=20, unique=True, default=ticket_reference, editable=False)
    organization = models.ForeignKey("organizations.Organization", on_delete=models.PROTECT, related_name="support_tickets")
    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="requested_support_tickets")
    pipeline = models.ForeignKey("crm.Pipeline", on_delete=models.SET_NULL, null=True, blank=True, related_name="support_tickets")
    # Snapshot only the support context, not unrelated CRM data or HTTP headers.
    context = models.JSONField(default=dict, editable=False)
    subject = models.CharField(max_length=200)
    category = models.ForeignKey(TicketCategory, on_delete=models.PROTECT)
    issue = models.ForeignKey(TicketIssue, on_delete=models.PROTECT)
    status = models.ForeignKey(TicketStatus, on_delete=models.PROTECT)
    priority = models.ForeignKey(TicketPriority, on_delete=models.PROTECT)
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="assigned_support_tickets")
    custom_values = models.JSONField(default=dict, blank=True)
    submission_key = models.UUIDField(null=True, blank=True)
    source = models.CharField(max_length=12, choices=[("portal", "Portal"), ("email", "Email")], default="portal")
    merged_into = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="merged_sources")
    first_staff_reply_at = models.DateTimeField(null=True, blank=True)
    last_public_activity_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(default=timezone.now)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ("-updated_at", "-id")
        indexes = [
            models.Index(fields=("organization", "-updated_at"), name="support_org_updated"),
            models.Index(fields=("assignee", "status"), name="support_assignee_status"),
            models.Index(fields=("status", "last_public_activity_at"), name="support_status_activity"),
        ]
        constraints = [
            models.CheckConstraint(condition=~models.Q(id=models.F("merged_into_id")), name="support_no_self_merge"),
            models.UniqueConstraint(fields=("requester", "submission_key"), name="support_submission_idempotency"),
        ]

    def clean(self):
        errors = {}
        if self.requester_id and self.requester.organization_id != self.organization_id:
            errors["requester"] = "Requester must belong to this organization."
        if self.pipeline_id and self.pipeline.organization_id != self.organization_id:
            errors["pipeline"] = "Pipeline must belong to this organization."
        if self.issue_id and self.issue.category_id != self.category_id:
            errors["issue"] = "Issue must belong to the selected category."
        if self.assignee_id:
            # Deactivating a historical assignee must not prevent customers replying.
            # Services and forms still require an ACTIVE platform identity for new assignments.
            current = (type(self).objects.filter(pk=self.pk).values_list("assignee_id", flat=True).first()
                       if not self._state.adding else None)
            if current != self.assignee_id and not platform_staff(self.assignee):
                errors["assignee"] = "Only active platform support staff can be assigned."
        if self.merged_into_id and self.merged_into.organization_id != self.organization_id:
            errors["merged_into"] = "Cross-organization merging is not allowed."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.reference} · {self.subject}"


class TicketMessage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="messages")
    origin_ticket = models.ForeignKey(Ticket, on_delete=models.PROTECT, related_name="original_messages")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    author_name = models.CharField(max_length=150)
    author_kind = models.CharField(max_length=12, choices=[("customer", "Customer"), ("staff", "Shvya-Ops"), ("shared", "Shared link")])
    internal = models.BooleanField(default=False)
    body = models.TextField(max_length=30000)
    created_at = models.DateTimeField(default=timezone.now)
    # Browser retry token is scoped to author + ticket (not globally guessable).
    client_key = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ("created_at", "id")
        constraints = [
            models.UniqueConstraint(fields=("ticket", "author", "client_key"), name="support_reply_idempotency"),
            models.UniqueConstraint(fields=("ticket", "client_key"), condition=models.Q(author__isnull=True), name="support_shared_reply_idempotency"),
        ]


class Attachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.ForeignKey(TicketMessage, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=attachment_path, storage=private_storage, max_length=300)
    original_name = models.CharField(max_length=180)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)


class TicketEvent(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=40)
    detail = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("-created_at", "-id")


class SharedAccess(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="share_links")
    token_hash = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=80, default="Shared viewer")
    can_reply = models.BooleanField(default=False)
    can_close = models.BooleanField(default=False)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(default=timezone.now)


class WorkItem(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="work_items")
    kind = models.CharField(max_length=12, choices=[("task", "Related task"), ("reminder", "Reminder")])
    title = models.CharField(max_length=200)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="support_work_items")
    due_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    notified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    def clean(self):
        super().clean()
        if self.assigned_to_id and not platform_staff(self.assigned_to):
            raise ValidationError("Work items can only be assigned to active platform staff.")


class SavedReply(NamedOption):
    body = models.TextField(max_length=10000)
    knowledge_url = models.URLField(blank=True, max_length=500)

    def clean(self):
        super().clean()
        if self.knowledge_url and not valid_knowledge_url(self.knowledge_url):
            raise ValidationError("Knowledge links must use HTTPS, without credentials.")


class EmailDelivery(models.Model):
    """Transactional outbox: committed before a worker sends SMTP."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(TicketEvent, on_delete=models.CASCADE)
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    state = models.CharField(max_length=12, default="pending", choices=[("pending", "Pending"), ("sending", "Sending"), ("sent", "Sent"), ("failed", "Failed"), ("skipped", "Skipped")])
    attempts = models.PositiveSmallIntegerField(default=0)
    available_at = models.DateTimeField(default=timezone.now)
    claimed_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=80, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("event", "recipient"), name="support_unique_delivery")]
        indexes = [models.Index(fields=("state", "available_at"), name="support_outbox_due")]


class InboundReceipt(models.Model):
    """Raw email bodies and credentials are deliberately not persisted in logs."""
    digest = models.CharField(max_length=64, primary_key=True)
    ticket = models.ForeignKey(Ticket, on_delete=models.SET_NULL, null=True, blank=True)
    result = models.CharField(max_length=24)
    reason = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(default=timezone.now)


class ConfigurationEvent(models.Model):
    """Staff configuration audit contains changed field names, never field values or secrets."""
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    section = models.CharField(max_length=40)
    object_key = models.CharField(max_length=64)
    changed_fields = models.JSONField(default=list)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("-created_at", "-id")
