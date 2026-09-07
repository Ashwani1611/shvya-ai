import uuid

from django.db import models


class HostedChatIgnoreContact(models.Model):
    """One contact that existed before a Hosted Account ignore-list snapshot.

    Rows are scoped to both organization and Hosted Account.  That distinction
    matters when an organization has more than one linked WhatsApp number: a
    contact that was already present on account A must not automatically be
    treated as an existing chat on account B.
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    organization = models.ForeignKey(
        "organizations.Organization",
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
    synced_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "channels"
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
                name="hosted_ign_org_phone_idx",
            ),
            models.Index(
                fields=["account", "phone_number"],
                name="hosted_ign_acc_phone_idx",
            ),
            models.Index(
                fields=["account", "chat_id"],
                name="hosted_ign_acc_chat_idx",
            ),
        ]

    def __str__(self):
        label = self.contact_name or self.phone_number
        return f"{label} — {self.account_id}"
