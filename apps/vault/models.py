import secrets
import uuid

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.db import models, transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .sections import ORIGIN_CHOICES, SECTION_CHOICES
from .storage import private_storage, vault_upload_path


def vault_slug():
    return secrets.token_urlsafe(18)


class Vault(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField("organizations.Organization", on_delete=models.CASCADE, related_name="vault")
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=80, unique=True, default=vault_slug)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    is_paused = models.BooleanField(default=False)
    access_code_hash = models.CharField(max_length=256)
    access_version = models.PositiveIntegerField(default=1)
    failed_access_attempts = models.PositiveSmallIntegerField(default=0)
    access_locked_until = models.DateTimeField(null=True, blank=True)
    token_hash = models.CharField(max_length=64, unique=True, null=True, blank=True, editable=False)
    token_prefix = models.CharField(max_length=16, blank=True, editable=False)
    token_created_at = models.DateTimeField(null=True, blank=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    storage_quota_bytes = models.PositiveBigIntegerField(default=2147483648)
    storage_used_bytes = models.PositiveBigIntegerField(default=0)
    submitted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.name

    def check_access_code(self, code):
        return check_password(str(code), self.access_code_hash)


class VaultSection(models.Model):
    class State(models.TextChoices):
        EMPTY = "empty", "Empty"
        FILLED = "filled", "Filled"
        DONT_HAVE = "dont_have", "We don't have this"

    vault = models.ForeignKey(Vault, on_delete=models.CASCADE, related_name="sections")
    key = models.CharField(max_length=24, choices=SECTION_CHOICES)
    state = models.CharField(max_length=16, choices=State.choices, default=State.EMPTY)
    is_done = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["vault", "key"], name="vault_section_unique")]


class VaultEntry(models.Model):
    class Kind(models.TextChoices):
        NOTE = "note", "Note"
        LINK = "link", "Link"
        FILE = "file", "File"
        AUDIO = "audio", "Voice note"

    class Author(models.TextChoices):
        CLIENT = "client", "Client"
        TEAM = "team", "SHVYA team"
        AGENT = "agent", "Agent"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    vault = models.ForeignKey(Vault, on_delete=models.CASCADE, related_name="entries")
    section = models.CharField(max_length=24, choices=SECTION_CHOICES)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.NOTE)
    body = models.TextField(blank=True)
    url = models.URLField(max_length=2048, blank=True)
    file = models.FileField(storage=private_storage, upload_to=vault_upload_path, blank=True, max_length=300)
    file_name = models.CharField(max_length=200, blank=True)
    file_size = models.PositiveBigIntegerField(default=0)
    mime_type = models.CharField(max_length=120, blank=True)
    author_type = models.CharField(max_length=16, choices=Author.choices, default=Author.CLIENT)
    origin = models.CharField(max_length=16, choices=ORIGIN_CHOICES, default="other")
    source_date = models.DateField(null=True, blank=True)
    external_id = models.CharField(max_length=200, null=True, blank=True)
    client_body = models.TextField(null=True, blank=True)
    client_edited_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    allowed_for_ai_sharing = models.BooleanField(default=False)
    send_when = models.TextField(blank=True)
    transcription_status = models.CharField(max_length=16, default="not_requested", choices=(("not_requested", "Not requested"), ("unavailable", "Unavailable"), ("provided", "Provided")))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_vault_entries")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["vault", "section"], name="vault_entry_section_idx")]
        constraints = [models.UniqueConstraint(fields=["vault", "author_type", "external_id"], condition=models.Q(external_id__isnull=False), name="vault_entry_external_unique")]

    @property
    def effective_body(self):
        return self.client_body if self.client_body is not None else self.body

    @property
    def source_label(self):
        if self.author_type == self.Author.CLIENT:
            return "Added by you"
        if self.origin == "fireflies":
            return f"From your call on {self.source_date.strftime('%d %b %Y')}" if self.source_date else "From your call"
        if self.origin == "whatsapp":
            return "From WhatsApp"
        return "SHVYA team"


class VaultEntryRevision(models.Model):
    entry = models.ForeignKey(VaultEntry, on_delete=models.CASCADE, related_name="revisions")
    body = models.TextField(blank=True)
    client_body = models.TextField(null=True, blank=True)
    metadata = models.JSONField(default=dict)
    actor_type = models.CharField(max_length=16)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class VaultQuestion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    vault = models.ForeignKey(Vault, on_delete=models.CASCADE, related_name="questions")
    section = models.CharField(max_length=24, choices=SECTION_CHOICES, default="other")
    text = models.TextField()
    answer = models.TextField(blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    external_id = models.CharField(max_length=200, null=True, blank=True)
    author_type = models.CharField(max_length=16, choices=VaultEntry.Author.choices, default="team")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [models.UniqueConstraint(fields=["vault", "author_type", "external_id"], condition=models.Q(external_id__isnull=False), name="vault_question_external_unique")]


class VaultCall(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    vault = models.ForeignKey(Vault, on_delete=models.CASCADE, related_name="calls")
    title = models.CharField(max_length=200)
    date = models.DateField()
    url = models.URLField(max_length=2048, blank=True)
    duration_min = models.PositiveIntegerField(null=True, blank=True)
    attendees = models.JSONField(default=list)
    summary = models.TextField(blank=True)
    external_id = models.CharField(max_length=200, null=True, blank=True)
    share_recording = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        constraints = [models.UniqueConstraint(fields=["vault", "external_id"], condition=models.Q(external_id__isnull=False), name="vault_call_external_unique")]


class VaultProfileSnapshot(models.Model):
    """Reviewable setup input. Never synchronizes production AI configuration."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    vault = models.ForeignKey(Vault, on_delete=models.CASCADE, related_name="profile_snapshots")
    body = models.JSONField(default=dict)
    status = models.CharField(max_length=16, default="draft", choices=(("draft", "Draft for review"), ("reviewed", "Reviewed")))
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class VaultEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    vault = models.ForeignKey(Vault, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=24, choices=(("created", "Created"), ("updated", "Client updated"), ("submitted", "Submitted"), ("access_rotated", "Access code changed"), ("token_rotated", "Agent token changed")))
    section = models.CharField(max_length=24, blank=True, choices=SECTION_CHOICES)
    actor_type = models.CharField(max_length=16, default="team")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


@receiver(post_delete, sender=VaultEntry)
def delete_private_vault_file(sender, instance, **kwargs):
    if instance.file and instance.file.name:
        storage, name = instance.file.storage, instance.file.name
        transaction.on_commit(lambda: storage.delete(name))
