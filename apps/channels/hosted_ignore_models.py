import uuid

from django.db import models

from apps.organizations.models import Organization


class HostedChatIgnoreContact(models.Model):
    """A pre-existing Hosted WhatsApp chat that must not auto-create a lead."""

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="hosted_chat_ignore_contacts",
    )
    account = models.ForeignKey(
        "channels.WhatsAppAccount",
        on_delete=models.CASCADE,
        related_name="ignored_existing_chats",
    )
    phone_number = models.CharField(max_length=32)
    contact_name = models.CharField(max_length=180, blank=True)
    chat_id = models.CharField(max_length=160, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["contact_name", "phone_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "phone_number"],
                name="uniq_hosted_ignore_account_phone",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "phone_number"],
                name="hosted_ignore_org_phone_idx",
            ),
            models.Index(
                fields=["account", "phone_number"],
                name="hosted_ignore_acct_phone_idx",
            ),
        ]

    def __str__(self):
        return self.contact_name or self.phone_number
