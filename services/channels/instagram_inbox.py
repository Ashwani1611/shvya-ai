"""Tenant-scoped inbox read model and standard-window reply policy.

Reuses the existing Instagram provider service and persisted models. No provider
requests occur while rendering or polling the inbox.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.channels.instagram_models import InstagramAccount, InstagramConversation, InstagramMessage
from services.channels import instagram_service as provider
from services.channels.instagram_content import display_attachments, reply_window, safe_error, safe_url
from services.channels.staging_outbound_safety import staging_outbound_allowed

PAGE_SIZE = 50
CURSOR_SALT = "instagram-inbox-history-v1"


def scoped_conversation(organization, conversation_id):
    try:
        conversation = InstagramConversation.objects.select_related("account").get(
            pk=conversation_id, organization=organization,
            account__organization=organization,
        )
    except (InstagramConversation.DoesNotExist, ValidationError, ValueError, TypeError):
        raise provider.InstagramAPIError("Instagram conversation was not found in this workspace.") from None
    return conversation


def conversation_policy(conversation):
    now = timezone.now()
    last_inbound = conversation.messages.filter(
        organization_id=conversation.organization_id,
        account_id=conversation.account_id,
        direction=InstagramMessage.Direction.INBOUND,
        sent_at__isnull=False, sent_at__lte=now,
    ).order_by("-sent_at").values_list("sent_at", flat=True).first()
    policy = reply_window(last_inbound, now)
    account = conversation.account
    if account.status != InstagramAccount.Status.CONNECTED or not account.access_token:
        policy.update(can_reply=False, reason="Reconnect Instagram before sending replies.")
    elif account.token_expires_at and account.token_expires_at <= now:
        policy.update(can_reply=False, reason="The Instagram access token expired. Reconnect Instagram.")
    elif not staging_outbound_allowed(conversation.participant_id):
        policy.update(can_reply=False, reason="Staging sends are disabled for this recipient. Use an allowlisted test account.")
    return policy


def assert_reply_allowed(conversation):
    policy = conversation_policy(conversation)
    if not policy["can_reply"]:
        raise provider.InstagramAPIError(policy["reason"])


def serialize_message(item):
    raw = item.raw_payload if isinstance(item.raw_payload, dict) else {}
    raw_message = raw.get("message") if isinstance(raw.get("message"), dict) else raw
    deleted = bool(raw_message.get("is_deleted"))
    media = display_attachments(item.attachments, raw)
    body = "Message deleted on Instagram" if deleted else item.body
    content = {"body": body, "media": media, "deleted": deleted}
    signature = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:20]
    return {
        "id": str(item.id), "external_id": item.external_id or "",
        "body": body, "direction": item.direction, "status": item.status,
        "created_time": (item.sent_at or item.created_at).isoformat(),
        "message_type": item.message_type, "media": media, "deleted": deleted,
        "content_version": signature,
        "error": "Message could not be sent. Check the connection and reply window." if item.error else "",
    }


def _conversation_dict(conversation):
    data = provider.serialize_conversation(conversation)
    if data["participant_name"] == "Instagram user" and conversation.participant_username:
        data["participant_name"] = conversation.participant_username
    data["participant_profile_picture_url"] = safe_url(conversation.participant_profile_picture_url)
    return data


def inbox_conversations(organization, *, query="", offset=0):
    account = provider.get_account(organization, include_disconnected=True)
    if not account:
        return {"conversations": [], "next_offset": None}
    latest = InstagramMessage.objects.filter(
        conversation_id=OuterRef("pk"), organization=organization, account=account,
    ).annotate(event_at=Coalesce("sent_at", "created_at")).order_by("-event_at", "-id")
    rows = InstagramConversation.objects.filter(
        organization=organization, account=account,
    ).annotate(
        inbox_at=Coalesce(Subquery(latest.values("event_at")[:1]), "last_message_at", "created_at"),
        inbox_body=Subquery(latest.values("body")[:1]),
        inbox_direction=Subquery(latest.values("direction")[:1]),
    )
    if query:
        rows = rows.filter(Q(participant_name__icontains=query) |
                           Q(participant_username__icontains=query) | Q(inbox_body__icontains=query) | Q(last_message_text__icontains=query))
    rows = list(rows.order_by("-inbox_at", "-id")[offset:offset + PAGE_SIZE + 1])
    result = []
    for row in rows[:PAGE_SIZE]:
        data = _conversation_dict(row)
        data["last_message_at"] = row.inbox_at.isoformat()
        data["last_message"] = (row.last_message_text if row.inbox_body is None else row.inbox_body) or "Instagram attachment"
        data["last_direction"] = row.inbox_direction or row.last_direction
        result.append(data)
    return {"conversations": result, "next_offset": offset + PAGE_SIZE if len(rows) > PAGE_SIZE else None}


def inbox_thread(organization, conversation_id, *, before=""):
    conversation = scoped_conversation(organization, conversation_id)
    rows = conversation.messages.filter(
        organization=organization, account_id=conversation.account_id,
    ).annotate(event_at=Coalesce("sent_at", "created_at"))
    if before:
        try:
            cursor = signing.loads(before, salt=CURSOR_SALT, max_age=86400)
            if cursor["org"] != str(organization.pk) or cursor["conversation"] != str(conversation.pk):
                raise ValueError
            stamp = parse_datetime(cursor["time"])
            identifier = uuid.UUID(cursor["id"])
            if stamp is None or stamp.tzinfo is None:
                raise ValueError
        except (signing.BadSignature, ValueError, TypeError, KeyError):
            raise provider.InstagramAPIError("This history cursor expired. Reopen the conversation.") from None
        rows = rows.filter(Q(event_at__lt=stamp) | Q(event_at=stamp, id__lt=identifier))
    page = list(rows.order_by("-event_at", "-id")[:PAGE_SIZE + 1])
    has_more = len(page) > PAGE_SIZE
    page = page[:PAGE_SIZE]
    cursor = ""
    if page and has_more:
        oldest = page[-1]
        cursor = signing.dumps({"org": str(organization.pk), "conversation": str(conversation.pk),
                                "time": oldest.event_at.isoformat(), "id": str(oldest.pk)},
                               salt=CURSOR_SALT, compress=True)
    data = _conversation_dict(conversation)
    data.update(messages=[serialize_message(item) for item in reversed(page)],
                before=cursor, policy=conversation_policy(conversation))
    return data


def mark_loaded_read(organization, conversation_id, message_ids):
    conversation = scoped_conversation(organization, conversation_id)
    with transaction.atomic():
        # Same lock used by our read/write endpoints, preventing lost counters.
        conversation = InstagramConversation.objects.select_for_update().get(
            pk=conversation.pk, organization=organization, account_id=conversation.account_id,
        )
        conversation.messages.filter(
            pk__in=message_ids, organization=organization, account_id=conversation.account_id,
            direction=InstagramMessage.Direction.INBOUND, is_read=False,
        ).update(is_read=True)
        unread = conversation.messages.filter(
            organization=organization, direction=InstagramMessage.Direction.INBOUND, is_read=False,
        ).count()
        InstagramConversation.objects.filter(pk=conversation.pk, organization=organization).update(unread_count=unread)


@transaction.atomic
def queue_inbox_reply(organization, *, conversation_id, body, idempotency_key=None):
    conversation = scoped_conversation(organization, conversation_id)
    # Serialize client retries per connected account. The existing UUID unique
    # constraint remains the final protection, including cross-account collisions.
    InstagramAccount.objects.select_for_update().get(pk=conversation.account_id, organization=organization)
    try:
        key = uuid.UUID(str(idempotency_key)) if idempotency_key else uuid.uuid4()
    except (TypeError, ValueError, AttributeError):
        raise provider.InstagramAPIError("Invalid message request ID. Reload this conversation.") from None
    body = str(body or "").strip()
    existing = InstagramMessage.objects.filter(organization=organization, idempotency_key=key).first()
    if existing:
        if (existing.organization_id != organization.pk or existing.conversation_id != conversation.pk
                or existing.direction != InstagramMessage.Direction.OUTBOUND or existing.body != body):
            raise provider.InstagramAPIError("This message request ID cannot be reused.")
        return existing
    assert_reply_allowed(conversation)
    try:
        with transaction.atomic():
            message = provider.queue_text_message(organization, conversation_id=conversation.pk, body=body)
            message.idempotency_key = key
            message.save(update_fields=["idempotency_key"])
    except IntegrityError:
        raise provider.InstagramAPIError("This message request ID cannot be reused.") from None
    return message


def claim_message(message_id):
    """Commit a durable send claim before external I/O; never auto-repeat it.

A timeout/crash after submission is ambiguous. A redelivered Celery task must
not send it again. A durable marker survives failures, unlike a short cache lock.
"""
    with transaction.atomic():
        message = InstagramMessage.objects.select_for_update().select_related(
            "account", "conversation", "conversation__account",
        ).filter(pk=message_id).first()
        if not message or message.status != InstagramMessage.Status.QUEUED:
            return None
        if (message.organization_id != message.account.organization_id or
                message.organization_id != message.conversation.organization_id or
                message.account_id != message.conversation.account_id or
                message.recipient_id != message.conversation.participant_id or
                message.sender_id != message.account.ig_user_id):
            raise provider.InstagramAPIError("Instagram message routing validation failed.")
        payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
        if payload.get("shvya_send_claimed_at"):
            return None
        assert_reply_allowed(message.conversation)
        message.raw_payload = {**payload, "shvya_send_claimed_at": timezone.now().isoformat()}
        message.save(update_fields=["raw_payload", "updated_at"])
        return message


def connection_error(error, account=None):
    secrets = (provider.instagram_app_secret(), account.access_token if account else "")
    return safe_error(error, secrets)
