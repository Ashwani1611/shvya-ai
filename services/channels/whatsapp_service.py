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
from django.db import models, transaction

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.providers import whatsapp as whatsapp_provider
from apps.channels.providers.whatsapp import WhatsAppAPIError, WhatsAppClient
from apps.crm.models import Lead, Pipeline, Stage
from services.channels.reply_intent_service import Intent, classify_reply
from services.crm.lead_service import upsert_lead
from services.crm.stage_service import move_to_next_stage


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


def resolve_account_for_lead(*, organization, lead):
    """
    Picks which connected WhatsAppAccount should be used to message
    a given lead, in priority order:

      1. Whichever account this lead has messaged with before
         (their most recent WhatsAppMessage) -- keeps a
         conversation on the same number it started on.
      2. The account matching the lead's pipeline's configured
         phone_number (Pipeline.phone_number), if set.
      3. The organization's first connected account, as a
         last-resort fallback so sending never hard-fails just
         because there's more than one number.

    Returns None if the organization has no connected account at all.
    """
    last_message = (
        WhatsAppMessage.objects.filter(
            lead=lead,
        )
        .select_related("account")
        .order_by("-created_at")
        .first()
    )

    if (
        last_message
        and last_message.account.status
        == last_message.account.Status.CONNECTED
    ):
        return last_message.account

    if lead.pipeline_id and lead.pipeline.phone_number:
        account = (
            WhatsAppAccount.objects.filter(
                organization=organization,
                is_active=True,
                status=WhatsAppAccount.Status.CONNECTED,
            )
            .filter(
                models.Q(
                    display_phone_number=lead.pipeline.phone_number
                )
                | models.Q(
                    phone_number_id=lead.pipeline.phone_number
                )
            )
            .first()
        )

        if account:
            return account

    return (
        WhatsAppAccount.objects.filter(
            organization=organization,
            is_active=True,
            status=WhatsAppAccount.Status.CONNECTED,
        )
        .first()
    )


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
    pipeline = (
        Pipeline.objects.filter(
            organization=organization,
            phone_number=to_number,
            is_active=True,
        )
        .first()
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
    """
    Queue internal conversation-summary generation for a Lead.

    The AI task is imported locally so the WhatsApp service does
    not create a module-level dependency on the AI task module.
    """

    from apps.ai_engagement.tasks import (
        generate_internal_conversation_summary,
    )

    generate_internal_conversation_summary.delay(
        str(lead_id)
    )

def _queue_whatsapp_engagement(
    *,
    lead_id,
):
    """
    Queue canonical AI Engagement execution for the Lead after the
    inbound WhatsApp transaction commits.

    The AI task receives only the Lead ID and resolves the current
    organization, stage, lead state, WhatsApp account, and latest
    conversation inside the worker.

    This keeps the queue payload minimal and prevents stale account
    state from being captured in the webhook transaction.
    """

    from apps.ai_engagement.tasks import (
        generate_ai_engagement_response,
    )

    generate_ai_engagement_response.delay(
        str(lead_id),
    )

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
    # RESOLVE / CREATE LEAD
    # --------------------------------------------------------

    lead = None

    pipeline = resolve_pipeline(
        organization=organization,
        to_number=to_number,
    )

    if pipeline:

        stage = _first_stage(
            pipeline
        )

        if stage:

            try:

                lead, _created = upsert_lead(
                    organization=organization,
                    pipeline=pipeline,
                    stage=stage,
                    name=from_number,
                    phone=from_number,
                    lead_source="whatsapp_api",
                )

            except DjangoValidationError:

                # Lead already exists under a different
                # pipeline/stage. Do not fight the existing
                # CRM assignment.

                lead = (
                    Lead.objects
                    .filter(
                        organization=organization,
                        phone=from_number,
                    )
                    .first()
                )

    # --------------------------------------------------------
    # CREATE INBOUND MESSAGE
    # --------------------------------------------------------

    message = (
        WhatsAppMessage.objects.create(
            organization=organization,
            account=account,
            lead=lead,
            direction=(
                WhatsAppMessage.Direction.INBOUND
            ),
            external_id=external_id,
            from_number=from_number,
            to_number=to_number,
            body=body,
            status=(
                WhatsAppMessage.Status.RECEIVED
            ),
            raw_payload=raw_payload,
            is_read=False,
        )
    )

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

        transaction.on_commit(
            lambda lead_id=lead_id: (
                _queue_whatsapp_engagement(
                    lead_id=lead_id,
                )
            )
        )

    return message


def _apply_reply_intent(
    *,
    lead,
    body,
):
    """
    A positive reply ("yes" / "+" / "interested" / etc.) auto-advances
    the lead to the next pipeline stage. A negative reply ("no" /
    "stop" / etc.) is tagged on the lead so agents/reporting can see
    it, but the lead is NOT auto-deleted or auto-moved backward --
    that decision stays with a human.

    Negative replies also feed the 24-hour no-response escalation:
    they don't count as "no response", but they do mean a human
    should follow up, which is handled by the calling-escalation
    task checking WhatsAppMessage history directly rather than a
    flag here.
    """
    intent = classify_reply(body)

    if intent == Intent.POSITIVE:

        move_to_next_stage(
            lead=lead,
        )

    elif intent == Intent.NEGATIVE:

        notes = lead.notes or ""

        marker = (
            "[WhatsApp] Lead replied negatively -- needs review."
        )

        if marker not in notes:

            lead.notes = (
                f"{notes}\n{marker}"
            ).strip()

            lead.save(
                update_fields=[
                    "notes",
                    "updated_at",
                ]
            )


def handle_status_update(
    *,
    external_id,
    status,
    raw_payload,
):
    """
    Update an outbound message's delivery status from a Meta
    status-callback webhook event (sent/delivered/read/failed).

    Existing SHVYA metadata in raw_payload is preserved so AI
    provenance remains available after delivery/read/failed events.

    Silently no-ops if we don't have a matching message.
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
        WhatsAppMessage.objects.filter(
            external_id=external_id,
        )
        .first()
    )

    if not message:
        return 0

    existing_payload = (
        message.raw_payload
        if isinstance(
            message.raw_payload,
            dict,
        )
        else {}
    )

    status_payload = (
        raw_payload
        if isinstance(
            raw_payload,
            dict,
        )
        else {}
    )

    final_payload = dict(
        status_payload
    )

    ai_metadata = (
        existing_payload.get(
            "shvya_ai"
        )
    )

    if ai_metadata is not None:

        final_payload["shvya_ai"] = (
            ai_metadata
        )

    message.status = mapped_status

    message.raw_payload = final_payload

    message.save(
        update_fields=[
            "status",
            "raw_payload",
            "updated_at",
        ]
    )

    return 1


# ============================================================
# OUTBOUND
# ============================================================


def queue_outbound_message(
    *,
    organization,
    account,
    to_number,
    body,
    lead=None,
):
    """
    Create the WhatsAppMessage row in `queued` status. The actual
    Meta API call happens later, in a Celery task
    (apps.channels.tasks.send_whatsapp_message_task) -- never
    synchronously in a view, per CLAUDE.md rule 3.
    """
    return WhatsAppMessage.objects.create(
        organization=organization,
        account=account,
        lead=lead,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        from_number=account.phone_number_id,
        to_number=to_number,
        body=body,
        status=WhatsAppMessage.Status.QUEUED,
    )


def send_outbound_message(
    *,
    message: WhatsAppMessage,
):
    """
    Actually call Meta's API for an already-queued WhatsAppMessage.

    Called from inside the Celery task, not directly from a view.

    Existing SHVYA metadata stored in raw_payload is preserved when
    Meta's response is recorded.
    """
    account = message.account

    client = WhatsAppClient(
        phone_number_id=account.phone_number_id,
        access_token=account.access_token,
    )

    try:

        response = client.send_text_message(
            to=message.to_number,
            body=message.body,
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

    external_id = None

    messages = (
        response.get("messages")
        or []
    )

    if messages:

        external_id = messages[0].get(
            "id"
        )

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

    final_payload = dict(
        response
    )

    if ai_metadata is not None:

        final_payload["shvya_ai"] = (
            ai_metadata
        )

    message.status = (
        WhatsAppMessage.Status.SENT
    )

    message.external_id = external_id

    message.raw_payload = final_payload

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
):
    """
    Returns one row per lead that has at least one WhatsApp
    message, ordered by most recent activity, each annotated with
    its last message and unread count. Used by the Chats inbox
    list view.
    """
    from django.db.models import Count, Max, Q

    messages = WhatsAppMessage.objects.filter(
        organization=organization,
        lead__isnull=False,
    )

    if account:
        messages = messages.filter(
            account=account
        )

    lead_ids = (
        messages
        .values_list(
            "lead_id",
            flat=True,
        )
        .distinct()
    )

    leads = (
        Lead.objects.filter(
            id__in=lead_ids
        )
        .annotate(
            last_message_at=Max(
                "whatsapp_messages__created_at",
                filter=(
                    Q(
                        whatsapp_messages__account=account
                    )
                    if account
                    else Q()
                ),
            ),
            unread_count=Count(
                "whatsapp_messages",
                filter=(
                    Q(
                        whatsapp_messages__direction=(
                            WhatsAppMessage.Direction.INBOUND
                        ),
                        whatsapp_messages__is_read=False,
                    )
                    & (
                        Q(
                            whatsapp_messages__account=account
                        )
                        if account
                        else Q()
                    )
                ),
            ),
        )
        .order_by(
            "-last_message_at"
        )
    )

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