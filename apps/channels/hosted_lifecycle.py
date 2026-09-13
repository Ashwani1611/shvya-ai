"""Lifecycle cleanup for Hosted WhatsApp linked-device accounts.

Hosted chat history belongs to the linked-device session. Once that session is
logged out/disconnected, keeping those rows makes the inbox look connected even
though the WhatsApp device has gone away. This module keeps that lifecycle
consistent and also owns permanent Hosted-account deletion cleanup.
"""

from copy import deepcopy
import logging

from django.core.cache import cache
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.organizations.models import Organization

logger = logging.getLogger(__name__)


HOSTED_CONNECTION_TYPE = "hosted"


def _delete_message_media(messages):
    """Best-effort removal of locally cached Hosted message attachments."""
    paths = set()
    for message in messages:
        payload = message.media_payload if isinstance(message.media_payload, dict) else {}
        path = str(payload.get("storage_path") or "").strip()
        if path:
            paths.add(path)

    for path in paths:
        try:
            if default_storage.exists(path):
                default_storage.delete(path)
        except Exception:
            logger.warning(
                "Could not delete Hosted WhatsApp media %s during account cleanup",
                path,
                exc_info=True,
            )


def clear_hosted_chat_history(*, account):
    """Delete only chat/message data owned by one Hosted WhatsApp account."""
    if account.connection_type != HOSTED_CONNECTION_TYPE:
        return 0

    queryset = WhatsAppMessage.objects.filter(
        organization_id=account.organization_id,
        account_id=account.id,
    )
    messages = list(queryset.only("id", "media_payload"))
    if messages:
        _delete_message_media(messages)
        queryset.delete()

    cache.delete(f"hosted-chat-history-refresh:{account.id}")

    # If the inbox is currently open, make it refresh immediately so the
    # disconnected session never keeps stale bubbles in the browser.
    try:
        from services.channels.hosted_chat_service import queue_hosted_chat_refresh

        queue_hosted_chat_refresh(
            account_id=account.id,
            reason="disconnected",
        )
    except Exception:
        logger.warning(
            "Could not broadcast Hosted chat cleanup for account %s",
            account.id,
            exc_info=True,
        )

    return len(messages)


def _remove_session_settings(*, account):
    organization = Organization.objects.select_for_update().get(
        pk=account.organization_id
    )
    settings = deepcopy(organization.settings or {})
    hosted_root = settings.get("hosted_whatsapp")
    if not isinstance(hosted_root, dict):
        return
    sessions = hosted_root.get("sessions")
    if not isinstance(sessions, dict):
        return

    if sessions.pop(str(account.id), None) is None:
        return

    if not sessions:
        hosted_root.pop("sessions", None)
    if not hosted_root:
        settings.pop("hosted_whatsapp", None)

    organization.settings = settings
    organization.save(update_fields=["settings", "updated_at"])


def delete_hosted_account(*, account):
    """Permanently delete a Hosted account and its account-owned data.

    The gateway logout is best-effort: an unreachable/already-gone browser
    session must not prevent an administrator from removing the stale account
    from SHVYA. Account-specific follow-up sequences are deleted first because
    that model intentionally protects its WhatsAppAccount foreign key.
    """
    if account.connection_type != HOSTED_CONNECTION_TYPE:
        raise ValueError("Only Hosted WhatsApp accounts can be deleted here.")

    gateway_error = ""
    try:
        from apps.channels.providers.whatsapp_web import (
            WhatsAppWebClient,
            WhatsAppWebGatewayError,
        )

        try:
            WhatsAppWebClient().logout(session_id=account.id)
        except WhatsAppWebGatewayError as exc:
            gateway_error = str(exc)
    except Exception as exc:
        gateway_error = str(exc)
        logger.warning(
            "Hosted gateway logout failed before deleting account %s",
            account.id,
            exc_info=True,
        )

    account_id = str(account.id)
    with transaction.atomic():
        locked = (
            WhatsAppAccount.objects.select_for_update()
            .filter(
                id=account.id,
                organization_id=account.organization_id,
                connection_type=HOSTED_CONNECTION_TYPE,
            )
            .first()
        )
        if not locked:
            return {
                "account_id": account_id,
                "deleted": False,
                "messages_deleted": 0,
                "gateway_error": gateway_error,
            }

        messages_deleted = clear_hosted_chat_history(account=locked)

        # FollowupSequence.whatsapp_account uses PROTECT. These sequences are
        # scoped to this exact sender, so remove their execution history first
        # and then the sequences as part of permanent account deletion.
        from apps.followups.models import FollowupExecution, FollowupSequence

        sequence_ids = list(
            FollowupSequence.objects.filter(whatsapp_account=locked).values_list(
                "id", flat=True
            )
        )
        if sequence_ids:
            FollowupExecution.objects.filter(sequence_id__in=sequence_ids).delete()
            FollowupSequence.objects.filter(id__in=sequence_ids).delete()

        _remove_session_settings(account=locked)
        locked.delete()

    return {
        "account_id": account_id,
        "deleted": True,
        "messages_deleted": messages_deleted,
        "gateway_error": gateway_error,
    }


@receiver(post_save, sender=WhatsAppAccount)
def clear_hosted_chats_when_disconnected(sender, instance, **kwargs):
    """Any real Hosted disconnect/log-out clears its persisted inbox."""
    if (
        instance.connection_type == HOSTED_CONNECTION_TYPE
        and instance.status == WhatsAppAccount.Status.DISCONNECTED
    ):
        clear_hosted_chat_history(account=instance)
