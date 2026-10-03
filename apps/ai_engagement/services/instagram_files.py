"""Deliver authored AI-guided documents through Instagram download links."""
from __future__ import annotations

from urllib.parse import urlsplit
from pathlib import Path

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.urls import reverse

from apps.ai_engagement.services.file_sharing import FileSharingError, FileSharingService


SHARED_FILE_SALT = "instagram-ai-guided-file-v1"
SHARED_FILE_MAX_AGE = 7 * 24 * 60 * 60
PROVIDER_FILE_SALT = "instagram-ai-provider-fetch-v1"
PROVIDER_FILE_MAX_AGE = 15 * 60


def document_for_message(message):
    """Recheck ownership and current sharing authorization before send/download."""
    payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    metadata = payload.get("shvya_ai")
    document_id = metadata.get("file_document_id") if isinstance(metadata, dict) else None
    if (
        str(message.organization_id) != str(message.account.organization_id)
        or str(message.organization_id) != str(message.conversation.organization_id)
        or message.account_id != message.conversation.account_id
        or message.recipient_id != message.conversation.participant_id
        or message.sender_id != message.account.ig_user_id
    ):
        raise FileSharingError("The selected file is no longer available.")
    document = FileSharingService().get_guided_document(
        organization=message.organization, document_id=document_id,
    )
    if document is None or metadata.get("file_version") != document.version:
        raise FileSharingError("The selected file is no longer available.")
    return document


def shared_file_url(message, *, provider_fetch=False):
    origin = str(getattr(settings, "OPERATIONS_PUBLIC_ORIGIN", "") or "").strip().rstrip("/")
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"} or not parsed.netloc
        or parsed.username or parsed.password or parsed.path
        or parsed.query or parsed.fragment
    ):
        raise FileSharingError("Configure the public application origin before sharing Instagram files.")
    salt = PROVIDER_FILE_SALT if provider_fetch else SHARED_FILE_SALT
    route = "ai-instagram-provider-file" if provider_fetch else "ai-instagram-shared-file"
    token = signing.TimestampSigner(salt=salt).sign(str(message.pk))
    return origin + reverse(route, kwargs={"token": token})


@transaction.atomic
def queue_guided_file_reply(*, organization, conversation, document_id, ai_metadata):
    from services.channels.instagram_inbox import queue_inbox_reply

    document = FileSharingService().get_guided_document(
        organization=organization, document_id=document_id,
    )
    if document is None:
        raise FileSharingError("The selected file is no longer available.")
    message = queue_inbox_reply(
        organization, conversation_id=conversation.pk,
        body=str(document.name or "Download file")[:255],
    )
    message.raw_payload = {
        **(message.raw_payload if isinstance(message.raw_payload, dict) else {}),
        "shvya_ai": {
            **ai_metadata,
            "file_document_id": document.pk,
            "file_version": document.version,
        },
    }
    message.body = f"{document.name or 'Download file'}\n{shared_file_url(message)}"
    if len(message.body) > 1000:
        raise FileSharingError("The file download link exceeds Instagram's message limit.")
    message.save(update_fields=["body", "raw_payload", "updated_at"])
    type(conversation).objects.filter(
        pk=conversation.pk, organization=organization,
    ).update(last_message_text=message.body)
    from apps.ai_engagement.services.instagram_delivery_outcomes import safely_record_instagram_delivery
    transaction.on_commit(lambda: safely_record_instagram_delivery(message.pk))
    return message


def guided_message_payload(message):
    document = document_for_message(message)
    if Path(document.file.name).suffix.lower() == ".pdf":
        return {"attachment": {"type": "file", "payload": {
            "url": shared_file_url(message, provider_fetch=True),
        }}}
    return {"text": message.body}
