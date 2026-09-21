"""
WhatsAppService -- business logic for inbound/outbound WhatsApp
messages lives here, never in views or the provider client.

Per CLAUDE.md:
  - rule 2: business logic belongs in services/, not views.
  - rule 3: the actual Meta API call happens inside a Celery task
            (apps.channels.tasks), never synchronously in a view.
  - rule 5: idempotency for webhook/task retries is enforced here
            via WhatsAppMessage.external_id (Meta's wamid).
"""

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.providers import whatsapp as whatsapp_provider
from apps.channels.providers.whatsapp import WhatsAppAPIError, WhatsAppClient
from apps.crm.models import Lead, Pipeline, Stage
from services.channels.reply_intent_service import Intent, classify_reply
from services.crm.lead_service import upsert_lead


class WhatsAppSendError(Exception):
    """Raised when an outbound message could not be sent."""


class WhatsAppEmbeddedSignupError(Exception):
    """Raised when the embedded signup callback couldn't be completed."""


# ============================================================
# ACCOUNT RESOLUTION
# ============================================================
#
# An organization can now have several connected WhatsApp numbers
# (see apps.channels.models.WhatsAppAccount -- ForeignKey, not
# OneToOne). Anywhere that used to grab "the" account with a bare
# .first() needs to instead pick the RIGHT one for a given lead.


def account_matches_lead_pipeline(*, account, lead):
    """Return True only when this account belongs to the lead's current pipeline."""
    if account is None or lead is None:
        return False
    if account.organization_id != lead.organization_id or not lead.pipeline_id:
        return False

    pipeline = lead.pipeline
    raw_pipeline_number = str(getattr(pipeline, "phone_number", "") or "").strip()
    if not raw_pipeline_number:
        return False

    # Some legacy pipeline rows store Meta's phone-number resource ID directly.
    if raw_pipeline_number == str(getattr(account, "phone_number_id", "") or "").strip():
        return True

    from services.channels.hosted_whatsapp_service import (
        normalize_whatsapp_number,
        pipeline_whatsapp_number,
    )

    expected_number = pipeline_whatsapp_number(pipeline)
    actual_number = normalize_whatsapp_number(
        phone_number=(
            getattr(account, "display_phone_number", "")
            or getattr(account, "phone_number_id", "")
        )
    )
    return bool(expected_number and actual_number and expected_number == actual_number)


def validate_account_for_lead_pipeline(*, account, lead):
    """Fail closed when an outbound lead message uses another pipeline's number."""
    if lead is None:
        return

    if account is None or account.organization_id != lead.organization_id:
        raise WhatsAppSendError(
            "WhatsApp account does not belong to the lead organization."
        )
    if not lead.pipeline_id:
        raise WhatsAppSendError("This lead is not assigned to a pipeline.")

    pipeline_number = str(getattr(lead.pipeline, "phone_number", "") or "").strip()
    if not pipeline_number:
        raise WhatsAppSendError(
            "This lead's current pipeline has no linked WhatsApp number."
        )
    if not account_matches_lead_pipeline(account=account, lead=lead):
        raise WhatsAppSendError(
            "This lead's current pipeline is linked to a different WhatsApp number. "
            "Message sending was blocked."
        )


def resolve_account_for_lead(*, organization, lead):
    """Return only the connected account linked to the lead's current pipeline.

    Conversation history never overrides the lead's current CRM pipeline, and
    there is deliberately no organization-level fallback.
    """
    if (
        organization is None
        or lead is None
        or lead.organization_id != organization.id
        or not lead.pipeline_id
    ):
        return None

    accounts = (
        WhatsAppAccount.objects.filter(
            organization=organization,
            is_active=True,
            status=WhatsAppAccount.Status.CONNECTED,
        )
        .order_by("-updated_at", "-pk")
    )
    for account in accounts:
        if account_matches_lead_pipeline(account=account, lead=lead):
            return account
    return None


# ============================================================
# EMBEDDED SIGNUP
# ============================================================


def complete_embedded_signup(
    *,
    organization,
    code,
    waba_id,
    phone_number_id,
):
    """
    Finishes the "Connect WhatsApp Now" flow after Meta's embedded
    signup popup hands the browser a `code`, `waba_id`, and
    `phone_number_id`. Trades the code for a token, fetches the
    number's display details, subscribes SHVYA's app to the WABA's
    webhooks, and creates/updates the WhatsAppAccount -- the same
    end state WhatsAppConnectAPIForm.save() reaches for the manual
    path, just without asking the person to type any of this in.

    Raises WhatsAppEmbeddedSignupError on any failure -- the view
    catches it and shows the message; nothing partial is saved
    (wrapped in a transaction) if a later step fails.
    """
    if not settings.META_APP_ID or not settings.META_APP_SECRET:
        raise WhatsAppEmbeddedSignupError(
            "Embedded signup isn't configured on this server yet "
            "(META_APP_ID / META_APP_SECRET missing)."
        )

    try:
        access_token = (
            whatsapp_provider.exchange_code_for_access_token(
                app_id=settings.META_APP_ID,
                app_secret=settings.META_APP_SECRET,
                code=code,
            )
        )

        phone_details = (
            whatsapp_provider.get_phone_number_details(
                phone_number_id=phone_number_id,
                access_token=access_token,
            )
        )

        # Not fatal on its own -- the number is still usable for
        # sending even if the subscribe call fails, it just means
        # inbound webhooks won't arrive until this is retried. Still
        # surfaced as an error so the person knows to fix it, rather
        # than silently connecting a half-working number.
        whatsapp_provider.subscribe_app_to_waba(
            waba_id=waba_id,
            access_token=access_token,
        )

    except WhatsAppAPIError as exc:
        raise WhatsAppEmbeddedSignupError(
            str(exc)
        ) from exc

    with transaction.atomic():
        account, _created = (
            WhatsAppAccount.objects.update_or_create(
                organization=organization,
                phone_number_id=phone_number_id,
                defaults={
                    "connection_type": (
                        WhatsAppAccount.ConnectionType.API
                    ),
                    "waba_id": waba_id,
                    "display_phone_number": phone_details.get(
                        "display_phone_number",
                        "",
                    ),
                    "business_name": phone_details.get(
                        "verified_name",
                        "",
                    ),
                    "access_token": access_token,
                    "status": (
                        WhatsAppAccount.Status.CONNECTED
                    ),
                },
            )
        )

    return account


# ============================================================
# PIPELINE / LEAD ROUTING
# ============================================================


def resolve_pipeline(
    *,
    organization,
    to_number,
):
    """
    Route an inbound message to a Pipeline based on which
    business number it was sent to (Pipeline.phone_number).

    Falls back to a pipeline named "Leads", then to the
    organization's first active pipeline, so a new inbound
    message never fails just because no number was configured.
    """
    from services.channels.hosted_whatsapp_service import resolve_pipeline_for_number

    pipeline = resolve_pipeline_for_number(
        organization=organization,
        phone_number=to_number,
    )

    if pipeline:
        return pipeline

    pipeline = (
        Pipeline.objects.filter(
            organization=organization,
            name="Leads",
            is_active=True,
        )
        .first()
    )

    if pipeline:
        return pipeline

    return (
        Pipeline.objects.filter(
            organization=organization,
            is_active=True,
        )
        .order_by("name")
        .first()
    )


def _first_stage(pipeline):
    return (
        Stage.objects.filter(
            pipeline=pipeline,
            is_active=True,
        )
        .order_by("display_order")
        .first()
    )


# ============================================================
# INTERNAL CONVERSATION SUMMARY TRIGGER
# ============================================================


def _queue_internal_conversation_summary(
    *,
    lead_id,
):
    """Summary refresh is signal-owned for both API and Hosted conversations."""
    return None


def _queue_whatsapp_engagement(
    *,
    lead_id,
):
    """Queue the durable, source-message-idempotent API AI execution path."""
    from apps.ai_engagement.services.execution_tracker import queue_api_engagement

    return queue_api_engagement(lead_id=lead_id)


# ============================================================
# INBOUND
# ============================================================


@transaction.atomic
def handle_inbound_message(
    *,
    organization,
    account,
    external_id,
    from_number,
    to_number,
    body,
    raw_payload,
):
    """
    Record an inbound WhatsApp message and attach it to a Lead,
    creating the Lead (and a pipeline/stage assignment) if this
    is the first time this number has messaged in.

    Idempotent on external_id -- Meta retries webhook deliveries,
    so a repeat call with the same wamid must be a no-op, not a
    duplicate row.

    After the inbound-message transaction commits successfully:

        1. Refresh the internal conversation summary.
        2. Queue the canonical AI Engagement task using Lead ID only.

    The AI worker resolves the current WhatsApp account and all
    other required state itself.
    """

    # --------------------------------------------------------
    # IDEMPOTENCY
    # --------------------------------------------------------

    existing = (
        WhatsAppMessage.objects
        .filter(
            external_id=external_id,
        )
        .first()
    )

    if existing:
        return existing

    # --------------------------------------------------------
    # NORMALIZE PHONE FOR LEAD LOOKUP
    # --------------------------------------------------------
    # Meta sends `from` without a leading "+" (e.g. "918360156287"),
    # but Lead.phone is stored normalized with "+". Keep the original
    # from_number unchanged for the WhatsApp message itself.
    normalized_lead_phone = (
        from_number
        if from_number.startswith("+")
        else f"+{from_number}"
    )

    # --------------------------------------------------------
    # RESOLVE / CREATE LEAD
    # --------------------------------------------------------

    from services.channels.hosted_whatsapp_service import get_session_settings

    # OFF prevents creation, not receipt of messages or attachment to an
    # existing CRM lead. Never reassign an existing lead's pipeline/stage.
    lead = Lead.objects.filter(
        organization=organization, phone=normalized_lead_phone,
    ).first()
    controls = get_session_settings(account=account)
    if lead is None and controls["auto_lead_creation"]:
        pipeline = resolve_pipeline(
            organization=organization,
            to_number=account.display_phone_number or to_number,
        )
        stage = _first_stage(pipeline) if pipeline else None
        if stage:
            try:
                lead, _created = upsert_lead(
                    organization=organization, pipeline=pipeline, stage=stage,
                    name=from_number, phone=normalized_lead_phone,
                    lead_source="whatsapp_api",
                )
            except DjangoValidationError:
                lead = Lead.objects.filter(
                    organization=organization, phone=normalized_lead_phone,
                ).first()

    # --------------------------------------------------------
    # CREATE INBOUND MESSAGE
    # --------------------------------------------------------
    # The fast lookup above avoids unnecessary CRM work on ordinary webhook
    # retries. It is not sufficient for true concurrency, though: two workers
    # can both observe "missing" before either inserts. The unique external_id
    # constraint is the durable authority, so the final insert must use
    # get_or_create. Django handles the losing unique-key insert inside a
    # savepoint and returns the row committed by the winner instead of leaking
    # IntegrityError to Meta.

    message_defaults = {
        "organization": organization,
        "account": account,
        "lead": lead,
        "direction": WhatsAppMessage.Direction.INBOUND,
        "from_number": from_number,
        "to_number": to_number,
        "body": body,
        "message_type": WhatsAppMessage.MessageType.TEXT,
        "media_payload": {},
        "status": WhatsAppMessage.Status.RECEIVED,
        "raw_payload": raw_payload,
        "is_read": False,
    }

    if external_id:
        message, created = WhatsAppMessage.objects.get_or_create(
            external_id=external_id,
            defaults=message_defaults,
        )
        if not created:
            return message
    else:
        # Preserve the existing behavior for malformed/legacy callers that do
        # not provide a provider message ID. Meta's normal inbound payloads do
        # provide one, and only those can participate in durable idempotency.
        message = WhatsAppMessage.objects.create(
            external_id=external_id,
            **message_defaults,
        )

    # The inbox is socket-driven; publishing must happen only after this
    # transaction commits, otherwise a connected browser can fetch a row that
    # is not visible yet.
    from services.channels.realtime import queue_message_publish
    queue_message_publish(message)

    # --------------------------------------------------------
    # EXISTING REPLY-INTENT LOGIC
    # --------------------------------------------------------

    if lead:

        _apply_reply_intent(
            lead=lead,
            body=body,
        )

        lead_id = str(
            lead.id
        )

        # ----------------------------------------------------
        # INTERNAL CONVERSATION SUMMARY
        #
        # Queue only after the inbound transaction commits so
        # the worker can see the newly-created WhatsAppMessage.
        # ----------------------------------------------------

        transaction.on_commit(
            lambda lead_id=lead_id: (
                _queue_internal_conversation_summary(
                    lead_id=lead_id,
                )
            )
        )

        # ----------------------------------------------------
        # AI ENGAGEMENT
        #
        # Phase 15:
        # Canonical task receives Lead ID only.
        #
        # Do NOT pass account_id here.
        #
        # The AI worker resolves the current WhatsApp account
        # itself using the existing account-resolution logic.
        # ----------------------------------------------------

        from services.channels.hosted_whatsapp_service import get_session_settings
        if get_session_settings(account=account).get("ai_auto_reply"):
            transaction.on_commit(
                lambda lead_id=lead_id: _queue_whatsapp_engagement(
                    lead_id=lead_id,
                )
            )

    return message


def _apply_reply_intent(
    *,
    lead,
    body,
):
    """Persist deterministic opt-out and negative-review intent only.

    Positive replies are valid qualification/conversation answers and must never
    move CRM stages implicitly. Stage movement is owned by validated AI/CRM
    action execution.
    """
    text = " ".join(str(body or "").strip().casefold().split())
    if not text:
        return

    from apps.ai_engagement.services.runtime_state import is_explicit_opt_out

    if is_explicit_opt_out(text):
        notes = lead.notes or ""
        marker = "[WhatsApp] Lead opted out of AI engagement."
        lead.ai_enabled = False
        if marker not in notes:
            lead.notes = f"{notes}\n{marker}".strip()
            lead.save(update_fields=["ai_enabled", "notes", "updated_at"])
        else:
            lead.save(update_fields=["ai_enabled", "updated_at"])
        return

    if classify_reply(body) == Intent.NEGATIVE:
        notes = lead.notes or ""
        marker = "[WhatsApp] Lead replied negatively -- needs review."
        if marker not in notes:
            lead.notes = f"{notes}\n{marker}".strip()
            lead.save(update_fields=["notes", "updated_at"])


def handle_status_update(
    *,
    external_id,
    status,
    raw_payload,
):
    """
    Update an outbound message's delivery status from a Meta
    status-callback webhook event (sent/delivered/read/failed).

    Silently no-ops if we don't have a matching message -- Meta
    may report statuses for messages sent before this system
    existed, or for read receipts on messages we didn't log.

    Preserve any SHVYA AI metadata already stored on the message.
    """

    status_map = {
        "sent": WhatsAppMessage.Status.SENT,
        "delivered": WhatsAppMessage.Status.DELIVERED,
        "read": WhatsAppMessage.Status.READ,
        "failed": WhatsAppMessage.Status.FAILED,
    }

    mapped_status = status_map.get(
        status
    )

    if not mapped_status:
        return None

    message = (
        WhatsAppMessage.objects
        .filter(
            external_id=external_id,
        )
        .first()
    )

    if not message:
        return 0

    existing_payload = (
        message.raw_payload
        if isinstance(message.raw_payload, dict)
        else {}
    )

    ai_metadata = existing_payload.get(
        "shvya_ai"
    )

    final_payload = (
        raw_payload
        if isinstance(raw_payload, dict)
        else {}
    )

    if ai_metadata is not None:
        final_payload = dict(
            final_payload
        )

        final_payload["shvya_ai"] = (
            ai_metadata
        )

    for key in ("shvya_welcome", "shvya_auto_followup", "shvya_workflow"):
        if key in existing_payload:
            final_payload = dict(final_payload)
            final_payload[key] = existing_payload[key]

    message.status = mapped_status
    message.raw_payload = final_payload

    message.save(
        update_fields=[
            "status",
            "raw_payload",
            "updated_at",
        ]
    )

    from services.channels.realtime import queue_status_publish
    queue_status_publish(message)

    return 1


# ============================================================
# OUTBOUND
# ============================================================


def _normalize_media_payload(
    media_payload,
):
    """
    Return a safe dictionary for queued outbound media metadata.
    """
    if media_payload is None:
        return {}

    if not isinstance(
        media_payload,
        dict,
    ):
        raise ValueError(
            "media_payload must be a dictionary."
        )

    return dict(
        media_payload
    )


def _validate_outbound_message_content(
    *,
    message_type,
    body,
    media_payload,
):
    """
    Deterministically validate the outbound WhatsApp content shape.

    This function does not call Meta and does not perform any
    database writes. It only validates the queued message contract.
    """
    allowed_types = {
        WhatsAppMessage.MessageType.TEXT,
        WhatsAppMessage.MessageType.IMAGE,
        WhatsAppMessage.MessageType.AUDIO,
        WhatsAppMessage.MessageType.VIDEO,
        WhatsAppMessage.MessageType.DOCUMENT,
    }

    if message_type not in allowed_types:
        raise ValueError(
            f"Unsupported WhatsApp message type: {message_type}"
        )

    media_payload = _normalize_media_payload(
        media_payload
    )

    # --------------------------------------------------------
    # TEXT
    # --------------------------------------------------------

    if message_type == WhatsAppMessage.MessageType.TEXT:

        if not (
            body
            or ""
        ).strip():
            raise ValueError(
                "Text WhatsApp messages require a non-empty body."
            )

        preview_url = media_payload.get(
            "preview_url",
            False,
        )

        if not isinstance(
            preview_url,
            bool,
        ):
            raise ValueError(
                "media_payload.preview_url must be a boolean."
            )

        return media_payload

    # --------------------------------------------------------
    # MEDIA
    # --------------------------------------------------------

    source = media_payload.get(
        "source"
    )

    if source not in {
        "document",
        "url",
        "media_id",
    }:
        raise ValueError(
            (
                "Media messages require source to be one of "
                "document, url, or media_id."
            )
        )

    if source == "document":

        document_id = media_payload.get(
            "document_id"
        )

        if (
            isinstance(
                document_id,
                bool,
            )
            or not isinstance(
                document_id,
                int,
            )
            or document_id <= 0
        ):
            raise ValueError(
                "document_id must be a positive integer."
            )

    elif source == "url":

        media_url = (
            media_payload.get(
                "url"
            )
            or ""
        ).strip()

        if not media_url:
            raise ValueError(
                "Media source=url requires a non-empty URL."
            )

    elif source == "media_id":

        media_id = (
            media_payload.get(
                "media_id"
            )
            or ""
        ).strip()

        if not media_id:
            raise ValueError(
                "Media source=media_id requires a non-empty media ID."
            )

    return media_payload


def queue_outbound_message(
    *,
    organization,
    account,
    to_number,
    body,
    lead=None,
    message_type=WhatsAppMessage.MessageType.TEXT,
    media_payload=None,
):
    """
    Create the WhatsAppMessage row in `queued` status.

    The actual Meta API call happens later in a Celery task.

    Existing callers that provide only `body` continue to create a
    normal text message.

    AI Engagement can additionally provide a message type and
    controlled media payload for documents, images, audio, or video.

    URL previews remain text messages and use:

        message_type=WhatsAppMessage.MessageType.TEXT
        media_payload={"preview_url": True}

    Organization-owned documents use:

        message_type=WhatsAppMessage.MessageType.DOCUMENT
        media_payload={
            "source": "document",
            "document_id": 123,
        }

    URL-backed media uses:

        message_type=WhatsAppMessage.MessageType.IMAGE
        media_payload={
            "source": "url",
            "url": "https://example.com/image.jpg",
        }

    Meta-uploaded media IDs use:

        message_type=WhatsAppMessage.MessageType.DOCUMENT
        media_payload={
            "source": "media_id",
            "media_id": "META_MEDIA_ID",
        }
    """
    if lead is not None:
        validate_account_for_lead_pipeline(account=account, lead=lead)

    normalized_media_payload = (
        _validate_outbound_message_content(
            message_type=message_type,
            body=body,
            media_payload=media_payload,
        )
    )

    message = WhatsAppMessage.objects.create(
        organization=organization,
        account=account,
        lead=lead,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        from_number=account.phone_number_id,
        to_number=to_number,
        body=body or "",
        message_type=message_type,
        media_payload=normalized_media_payload,
        status=WhatsAppMessage.Status.QUEUED,
    )
    # Show the queued outbound bubble immediately; delivery/status updates are
    # published separately when Meta responds.
    from services.channels.realtime import queue_message_publish
    queue_message_publish(message)
    return message


def _send_outbound_media_message(
    *,
    client,
    message,
):
    """
    Resolve and send one queued non-text WhatsApp media message.

    Organization-owned SHVYA Documents are validated again at send
    time so a stale AI decision cannot send an inactive, incomplete,
    or cross-organization file.
    """
    media_type = message.message_type

    payload = _normalize_media_payload(
        message.media_payload
    )

    source = payload.get(
        "source"
    )

    if media_type == WhatsAppMessage.MessageType.TEXT:
        raise ValueError(
            "_send_outbound_media_message cannot send text content."
        )

    # ========================================================
    # ORGANIZATION-OWNED SHVYA DOCUMENT
    # ========================================================

    if source == "document":

        from apps.ai_engagement.models import Document

        document_id = payload.get(
            "document_id"
        )

        document = (
            Document.objects
            .filter(
                id=document_id,
                organization=message.organization,
                is_active=True,
                processing_status=(
                    Document.ProcessingStatus.COMPLETED
                ),
            )
            .exclude(
                file="",
            )
            .first()
        )

        if not document:
            raise ValueError(
                (
                    "The requested WhatsApp document is not an "
                    "eligible organization-owned file."
                )
            )

        if not document.file:
            raise ValueError(
                "The requested WhatsApp document has no file."
            )

        filename = (
            payload.get(
                "filename"
            )
            or document.file.name.rsplit(
                "/",
                1,
            )[-1]
        )

        caption = (
            payload.get(
                "caption"
            )
            if "caption" in payload
            else message.body
        )

        import mimetypes

        mime_type = (
            payload.get(
                "mime_type"
            )
            or mimetypes.guess_type(
                filename
            )[0]
            or "application/octet-stream"
        )

        document.file.open(
            "rb"
        )

        try:
            upload_response = client.upload_media(
                file_obj=document.file.file,
                filename=filename,
                mime_type=mime_type,
            )

        finally:
            document.file.close()

        media_id = upload_response.get(
            "id"
        )

        if not media_id:
            raise ValueError(
                "Meta media upload returned no media ID."
            )

        return client.send_media_message(
            to=message.to_number,
            media_type=media_type,
            media_id=media_id,
            caption=caption,
            filename=(
                filename
                if media_type
                == WhatsAppMessage.MessageType.DOCUMENT
                else None
            ),
        )

    # ========================================================
    # URL-BACKED MEDIA
    # ========================================================

    if source == "url":

        media_url = (
            payload.get(
                "url"
            )
            or ""
        ).strip()

        from urllib.parse import urlparse

        parsed = urlparse(
            media_url
        )

        if (
            parsed.scheme
            not in {
                "http",
                "https",
            }
            or not parsed.netloc
        ):
            raise ValueError(
                "WhatsApp media URLs must be absolute HTTP(S) URLs."
            )

        return client.send_media_message(
            to=message.to_number,
            media_type=media_type,
            media_url=media_url,
            caption=(
                payload.get(
                    "caption",
                )
                if "caption" in payload
                else message.body
            ),
            filename=payload.get(
                "filename",
            ),
        )

    # ========================================================
    # META MEDIA ID
    # ========================================================

    if source == "media_id":

        media_id = (
            payload.get(
                "media_id"
            )
            or ""
        ).strip()

        return client.send_media_message(
            to=message.to_number,
            media_type=media_type,
            media_id=media_id,
            caption=(
                payload.get(
                    "caption",
                )
                if "caption" in payload
                else message.body
            ),
            filename=payload.get(
                "filename",
            ),
        )

    raise ValueError(
        "Unsupported WhatsApp media source."
    )


def send_outbound_message(
    *,
    message: WhatsAppMessage,
):
    """
    Actually call Meta's API for an already-queued WhatsAppMessage.

    Called from inside the Celery task, not directly from a view.

    Transport selection is deterministic:

        text      -> Meta text message

        document  -> validated SHVYA Document
                     -> Meta media upload
                     -> Meta document message

        image     -> Meta image message

        audio     -> Meta audio message

        video     -> Meta video message

    URL previews remain text messages with preview_url enabled.

    Preserve any SHVYA AI metadata stored on the queued message
    while recording Meta's outbound API response.
    """

    account = message.account

    # --------------------------------------------------------
    # ACCOUNT SAFETY
    # --------------------------------------------------------

    if (
        account.organization_id
        != message.organization_id
    ):
        raise WhatsAppSendError(
            "WhatsApp account does not belong to the message organization."
        )

    if not account.is_active:
        raise WhatsAppSendError(
            "WhatsApp account is inactive."
        )

    if (
        account.status
        != WhatsAppAccount.Status.CONNECTED
    ):
        raise WhatsAppSendError(
            "WhatsApp account is not connected."
        )

    # Re-check the live CRM pipeline immediately before the provider call.
    # This blocks stale queued jobs when a lead has moved to another pipeline.
    if message.lead_id:
        validate_account_for_lead_pipeline(account=account, lead=message.lead)

    payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    ai_metadata = payload.get("shvya_ai") or {}
    if ai_metadata:
        from services.channels.hosted_whatsapp_service import account_ai_block_reason

        reason = account_ai_block_reason(
            account=account, lead=message.lead,
            bump_up_number=(ai_metadata.get("number", 1) if ai_metadata.get("origin") == "bump_up" else None),
        )
        if reason:
            message.status = WhatsAppMessage.Status.FAILED
            message.error = f"AI send cancelled: {reason}"
            message.save(update_fields=["status", "error", "updated_at"])
            raise WhatsAppSendError(message.error)

    client = WhatsAppClient(
        phone_number_id=account.phone_number_id,
        access_token=account.access_token,
    )

    # --------------------------------------------------------
    # TRANSPORT
    # --------------------------------------------------------

    try:

        message_type = message.message_type

        media_payload = (
            _validate_outbound_message_content(
                message_type=message_type,
                body=message.body,
                media_payload=message.media_payload,
            )
        )

        # ----------------------------------------------------
        # TEXT
        # ----------------------------------------------------

        if (
            message_type
            == WhatsAppMessage.MessageType.TEXT
        ):

            response = (
                client.send_text_message(
                    to=message.to_number,
                    body=message.body,
                    preview_url=(
                        media_payload.get(
                            "preview_url",
                            False,
                        )
                    ),
                )
            )

        # ----------------------------------------------------
        # MEDIA
        # ----------------------------------------------------

        else:

            response = (
                _send_outbound_media_message(
                    client=client,
                    message=message,
                )
            )

    except WhatsAppAPIError as exc:

        message.status = (
            WhatsAppMessage.Status.FAILED
        )

        message.error = str(
            exc
        )

        message.save(
            update_fields=[
                "status",
                "error",
                "updated_at",
            ]
        )

        raise WhatsAppSendError(
            str(exc)
        ) from exc

    except (
        ValueError,
        OSError,
    ) as exc:

        message.status = (
            WhatsAppMessage.Status.FAILED
        )

        message.error = str(
            exc
        )

        message.save(
            update_fields=[
                "status",
                "error",
                "updated_at",
            ]
        )

        raise WhatsAppSendError(
            str(exc)
        ) from exc

    # --------------------------------------------------------
    # META RESPONSE
    # --------------------------------------------------------

    external_id = None

    messages = (
        response.get(
            "messages"
        )
        or []
    )

    if messages:

        external_id = (
            messages[0].get(
                "id"
            )
        )

    # --------------------------------------------------------
    # PRESERVE SHVYA AI METADATA
    # --------------------------------------------------------

    existing_payload = (
        message.raw_payload
        if isinstance(
            message.raw_payload,
            dict,
        )
        else {}
    )

    ai_metadata = (
        existing_payload.get(
            "shvya_ai"
        )
    )

    final_payload = (
        response
        if isinstance(
            response,
            dict,
        )
        else {}
    )

    if ai_metadata is not None:

        final_payload = dict(
            final_payload
        )

        final_payload["shvya_ai"] = (
            ai_metadata
        )

    # --------------------------------------------------------
    for key in ("shvya_welcome", "shvya_auto_followup", "shvya_workflow"):
        if key in existing_payload:
            final_payload = dict(final_payload)
            final_payload[key] = existing_payload[key]

    # MARK SENT
    # --------------------------------------------------------

    message.status = (
        WhatsAppMessage.Status.SENT
    )

    message.external_id = (
        external_id
    )

    message.raw_payload = (
        final_payload
    )

    message.save(
        update_fields=[
            "status",
            "external_id",
            "raw_payload",
            "updated_at",
        ]
    )

    return message


# ============================================================
# CONVERSATIONS (Chats inbox)
# ============================================================


def list_conversations(
    *,
    organization,
    account=None,
    tab="all",
):
    """
    Returns one row per lead that has at least one WhatsApp
    message, ordered by most recent activity, each annotated with
    last message preview, direction, status, and unread count.

    tab: "all" | "unread" | "needs_reply" | "failed" | "broadcasts"
    """
    from django.db.models import Count, Max, OuterRef, Q, Subquery

    acc_q = Q(whatsapp_messages__organization=organization)
    if account:
        acc_q &= Q(whatsapp_messages__account=account)

    base_msg_qs = WhatsAppMessage.objects.filter(
        organization=organization,
        lead__isnull=False,
    )
    if account:
        base_msg_qs = base_msg_qs.filter(account=account)

    lead_ids = base_msg_qs.values_list("lead_id", flat=True).distinct()

    # Subquery: last message fields per lead
    last_msg_qs = base_msg_qs.filter(lead=OuterRef("pk")).order_by("-created_at", "-pk")

    leads = (
        Lead.objects.filter(organization=organization, id__in=lead_ids)
        .annotate(
            last_message_at=Max(
                "whatsapp_messages__created_at",
                filter=acc_q,
            ),
            unread_count=Count(
                "whatsapp_messages",
                filter=(
                    Q(
                        whatsapp_messages__direction=WhatsAppMessage.Direction.INBOUND,
                        whatsapp_messages__is_read=False,
                    )
                    & acc_q
                ),
            ),
            failed_count=Count(
                "whatsapp_messages",
                filter=(
                    Q(whatsapp_messages__status=WhatsAppMessage.Status.FAILED)
                    & acc_q
                ),
            ),
        )
        .annotate(
            last_msg_body=Subquery(last_msg_qs.values("body")[:1]),
            last_msg_direction=Subquery(last_msg_qs.values("direction")[:1]),
            last_msg_status=Subquery(last_msg_qs.values("status")[:1]),
            last_msg_error=Subquery(last_msg_qs.values("error")[:1]),
        )
        .order_by("-last_message_at")
    )

    # Tab filters
    if tab == "unread":
        leads = leads.filter(unread_count__gt=0)
    elif tab == "needs_reply":
        leads = leads.filter(last_msg_direction=WhatsAppMessage.Direction.INBOUND)
    elif tab == "failed":
        leads = leads.filter(last_msg_status=WhatsAppMessage.Status.FAILED)
    elif tab == "broadcasts":
        broadcast_lead_ids = (
            WhatsAppMessage.objects.filter(
                organization=organization,
                bulk_recipient__isnull=False,
                **({"account": account} if account else {}),
            )
            .values_list("lead_id", flat=True)
            .distinct()
        )
        leads = leads.filter(id__in=broadcast_lead_ids)

    return leads


def get_conversation_messages(
    *,
    organization,
    lead,
    account=None,
):
    """
    Full message thread for one lead, oldest first (chat order).
    """
    messages = WhatsAppMessage.objects.filter(
        organization=organization,
        lead=lead,
    )

    if account:
        messages = messages.filter(
            account=account
        )

    return messages.order_by(
        "created_at"
    )


def mark_conversation_read(
    *,
    organization,
    lead,
):
    """
    Marks every unread inbound message for this lead as read --
    called when an agent opens the conversation.
    """
    return (
        WhatsAppMessage.objects.filter(
            organization=organization,
            lead=lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            is_read=False,
        )
        .update(
            is_read=True
        )
    )
