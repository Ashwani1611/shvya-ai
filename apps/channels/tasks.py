"""
Celery tasks for outbound WhatsApp sends -- single messages and
bulk campaigns.

NOTE: Celery is scaffolded (config/celery.py) but not yet wired into
INSTALLED_APPS/config/__init__.py or given CELERY_BROKER_URL in
settings -- see the roadmap item "Add a Celery worker + beat service
to docker-compose.yml". Until that's done, calling
.delay()/.apply_async() on these tasks will raise, not silently
no-op. Write and reference them now so the send flow is correct the
moment that wiring lands.
"""
import logging

from celery import chord, group, shared_task
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# Internal transient state used only while the provider request is in flight.
# It is intentionally not a user-selectable model choice. Persisting the claim
# before the network call prevents concurrent workers from sending the same
# queued row while keeping the external HTTP request outside a DB transaction.
_WHATSAPP_SENDING_STATUS = "sending"


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def sync_whatsapp_templates_task(self, account_id):
    """Refresh one connected Cloud API account after a Meta template event.

    Meta's webhook only tells us that a template changed; the Graph API remains
    the authoritative source for its complete definition and for remote deletes.
    Keeping that fetch out of the webhook request makes Meta acknowledgements
    fast and retryable.
    """
    from apps.channels.models import WhatsAppAccount
    from services.channels.template_service import TemplateError
    from services.channels.template_meta_fix import sync_templates

    account = (
        WhatsAppAccount.objects.select_related("organization")
        .filter(
            id=account_id,
            connection_type=WhatsAppAccount.ConnectionType.API,
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        .first()
    )
    if not account:
        return {"status": "skipped", "reason": "account_not_connected"}

    try:
        summary = sync_templates(
            organization=account.organization,
            account=account,
        )
    except TemplateError as exc:
        logger.warning(
            "Template sync after Meta webhook failed for account %s: %s",
            account_id,
            exc,
        )
        raise self.retry(exc=exc)

    return {"status": "synced", **summary}


# ============================================================
# SINGLE MESSAGE SEND
# ============================================================


def _requeue_whatsapp_message_after_transient_failure(*, message_id):
    """Return an in-flight/failed message to QUEUED before Celery retries it."""
    from apps.channels.models import WhatsAppMessage

    with transaction.atomic():
        message = (
            WhatsAppMessage.objects.select_for_update()
            .filter(id=message_id)
            .first()
        )
        if message is None:
            return
        if message.status in (
            WhatsAppMessage.Status.SENT,
            WhatsAppMessage.Status.DELIVERED,
            WhatsAppMessage.Status.READ,
        ):
            return

        message.status = WhatsAppMessage.Status.QUEUED
        message.error = ""
        message.save(
            update_fields=[
                "status",
                "error",
                "updated_at",
            ]
        )


def _persist_whatsapp_message_failure(*, message_id, error):
    """Persist a terminal failure outside the provider-call transaction."""
    from apps.channels.models import WhatsAppMessage

    with transaction.atomic():
        message = (
            WhatsAppMessage.objects.select_for_update()
            .filter(id=message_id)
            .first()
        )
        if message is None:
            return
        if message.status in (
            WhatsAppMessage.Status.SENT,
            WhatsAppMessage.Status.DELIVERED,
            WhatsAppMessage.Status.READ,
        ):
            return

        update_fields = []
        if message.status != WhatsAppMessage.Status.FAILED:
            message.status = WhatsAppMessage.Status.FAILED
            update_fields.append("status")
        if not message.error:
            message.error = str(error)[:1000]
            update_fields.append("error")
        if update_fields:
            update_fields.append("updated_at")
            message.save(update_fields=update_fields)


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def send_whatsapp_message_task(self, message_id):
    """
    Send an already-queued WhatsAppMessage via Meta's API.

    Idempotency:
        - SENT, DELIVERED, and READ messages are never sent again.
        - Only QUEUED messages are eligible to enter the send path.
        - A short select_for_update() transaction atomically claims a
          QUEUED row as the internal ``sending`` state.
        - The provider HTTP request runs only after that transaction commits,
          so no database row lock is held while waiting on Meta/Hosted.

    Retries:
        - Network failures and Meta 5xx responses are returned to QUEUED
          before Celery retries them.
        - Meta 4xx and other permanent failures remain durably FAILED.
    """
    from apps.channels.models import WhatsAppMessage
    from apps.channels.providers.whatsapp import WhatsAppAPIError
    from services.channels.whatsapp_service import (
        WhatsAppSendError,
        send_outbound_message,
    )

    # --------------------------------------------------------
    # RESOLVE + CLAIM MESSAGE
    # --------------------------------------------------------
    # Keep this transaction deliberately short. The external provider call
    # happens below, after commit, so a slow Meta/Hosted request cannot hold a
    # PostgreSQL row lock or roll back a failure status written by the sender.

    try:
        with transaction.atomic():
            message = (
                WhatsAppMessage.objects.select_for_update()
                .select_related("account")
                .get(id=message_id)
            )

            if message.status in (
                WhatsAppMessage.Status.SENT,
                WhatsAppMessage.Status.DELIVERED,
                WhatsAppMessage.Status.READ,
            ):
                logger.info(
                    "send_whatsapp_message_task: "
                    "message %s already sent; skipping duplicate send",
                    message_id,
                )
                return {
                    "status": "skipped",
                    "reason": "already_sent",
                    "message_id": str(message_id),
                }

            if message.status != WhatsAppMessage.Status.QUEUED:
                logger.info(
                    "send_whatsapp_message_task: "
                    "message %s has status %s; skipping send",
                    message_id,
                    message.status,
                )
                return {
                    "status": "skipped",
                    "reason": "message_not_queued",
                    "message_id": str(message_id),
                }

            # Hosted AI creates the same durable WhatsAppMessage row, but its
            # delivery is owned by HostedAutomationJob. Suppress only the AI
            # finalizer's canonical sender call. Hosted agent/manual messages
            # must keep flowing through the provider-aware canonical task.
            payload = (
                message.raw_payload
                if isinstance(message.raw_payload, dict)
                else {}
            )
            if (
                message.account.connection_type == "hosted"
                and payload.get("shvya_ai")
            ):
                logger.info(
                    "send_whatsapp_message_task: "
                    "message %s is Hosted AI; leaving delivery to "
                    "Hosted automation",
                    message_id,
                )
                return {
                    "status": "skipped",
                    "reason": "hosted_ai_transport_managed_separately",
                    "message_id": str(message_id),
                }

            if "shvya_workflow" in payload:
                from services.triggers.actions import (
                    defer_workflow_message_for_health,
                    workflow_message_block_reason,
                )

                reason = workflow_message_block_reason(message)
                if reason:
                    message.status = WhatsAppMessage.Status.FAILED
                    message.error = f"Workflow send cancelled: {reason}"
                    message.save(update_fields=["status", "error", "updated_at"])
                    return {"status": "skipped", "reason": reason, "message_id": str(message_id)}
                if defer_workflow_message_for_health(message):
                    return {"status": "deferred", "reason": "account_health", "message_id": str(message_id)}

            message.status = _WHATSAPP_SENDING_STATUS
            message.error = ""
            message.save(
                update_fields=[
                    "status",
                    "error",
                    "updated_at",
                ]
            )

    except WhatsAppMessage.DoesNotExist:
        logger.warning(
            "send_whatsapp_message_task: message %s not found",
            message_id,
        )
        return

    # --------------------------------------------------------
    # PROVIDER CALL -- NO DATABASE TRANSACTION / ROW LOCK
    # --------------------------------------------------------

    try:
        send_outbound_message(message=message)
    except WhatsAppSendError as exc:
        original = exc.__cause__

        if "shvya_workflow" in payload and isinstance(original, WhatsAppAPIError):
            from apps.triggers.models import TriggerRun
            from services.triggers.actions import defer_workflow_message_for_health

            if original.status_code == 503 and defer_workflow_message_for_health(message):
                _requeue_whatsapp_message_after_transient_failure(message_id=message_id)
                return {"status": "deferred", "reason": "account_health", "message_id": str(message_id)}

            # A timeout may follow a successful provider send. Never resend an
            # uncertain workflow automatically. Explicit 5xx responses retain
            # the canonical bounded retry path; exhausting it is terminal.
            uncertain = original.status_code is None
            exhausted = self.request.retries >= self.max_retries
            if uncertain or exhausted:
                _persist_whatsapp_message_failure(message_id=message_id, error=exc)
                TriggerRun.objects.filter(
                    message_id=message_id, status__in=["queued", "dispatching"]
                ).update(
                    status="needs_review" if uncertain else "failed",
                    detail=("Provider outcome is uncertain. Check delivery before retrying."
                            if uncertain else "WhatsApp retry limit reached."),
                    finished_at=timezone.now(),
                )
                return {"status": "needs_review" if uncertain else "failed", "message_id": str(message_id)}

        if isinstance(original, WhatsAppAPIError) and (
            original.status_code is None
            or original.status_code >= 500
        ):
            _requeue_whatsapp_message_after_transient_failure(
                message_id=message_id
            )
            logger.warning(
                "send_whatsapp_message_task: "
                "transient failure for message %s; retrying: %s",
                message_id,
                exc,
            )
            raise self.retry(
                exc=exc,
                countdown=30,
            )

        _persist_whatsapp_message_failure(
            message_id=message_id,
            error=exc,
        )
        logger.error(
            "send_whatsapp_message_task: "
            "permanent failure for message %s: %s "
            "(meta response: %s)",
            message_id,
            exc,
            getattr(original, "response_body", None),
        )
        return {
            "status": "failed",
            "reason": "permanent_send_failure",
            "message_id": str(message_id),
            "error": str(exc),
        }
    except Exception as exc:
        # Defensive terminalization for programming/provider-wrapper failures.
        # Without this, an unexpected exception after the claim could leave the
        # message indefinitely in the internal in-flight state.
        _persist_whatsapp_message_failure(
            message_id=message_id,
            error=exc,
        )
        logger.exception(
            "send_whatsapp_message_task: unexpected failure for message %s",
            message_id,
        )
        raise

    return {
        "status": "sent",
        "message_id": str(message_id),
    }


# ============================================================
# BULK CAMPAIGN SEND
# ============================================================


def _mark_bulk_recipient_failed(*, recipient_id, message_id, reason):
    """Persist one terminal bulk-recipient failure without replacing its message."""
    from apps.channels.models import BulkMessageRecipient

    with transaction.atomic():
        recipient = (
            BulkMessageRecipient.objects.select_for_update()
            .filter(id=recipient_id)
            .first()
        )
        if recipient is None or recipient.message_id != message_id:
            return
        if recipient.status == BulkMessageRecipient.Status.SENT:
            return

        recipient.status = BulkMessageRecipient.Status.FAILED
        recipient.skip_reason = str(reason)[:200]
        recipient.save(
            update_fields=[
                "status",
                "skip_reason",
                "updated_at",
            ]
        )


@shared_task(
    bind=True,
    max_retries=2,
    default_retry_delay=15,
    rate_limit="10/s",
)
def send_bulk_recipient_task(self, recipient_id):
    """Send one durable WhatsApp message for one campaign recipient.

    The recipient row is the idempotency boundary. A short row-locking
    transaction creates and attaches at most one WhatsAppMessage, then claims
    that message before the provider request. Retries always reuse the attached
    message instead of creating another outbound row.

    Explicit Meta 5xx responses are retried with the same message. Network
    errors with no HTTP response are intentionally terminal for bulk sends:
    Meta may already have accepted the request, so blindly retrying an unknown
    outcome can deliver the campaign message twice to the customer.
    """
    from apps.channels.models import BulkMessageRecipient, WhatsAppMessage
    from apps.channels.providers.whatsapp import WhatsAppAPIError
    from services.channels.bulk_service import is_within_24h_window
    from services.channels.whatsapp_service import (
        WhatsAppSendError,
        queue_outbound_message,
        send_outbound_message,
    )

    try:
        with transaction.atomic():
            recipient = (
                BulkMessageRecipient.objects.select_for_update()
                .select_related(
                    "campaign",
                    "campaign__account",
                    "lead",
                )
                .get(id=recipient_id)
            )
            campaign = recipient.campaign
            lead = recipient.lead

            if recipient.status == BulkMessageRecipient.Status.SENT:
                return {
                    "status": "skipped",
                    "reason": "already_sent",
                    "recipient_id": str(recipient_id),
                }

            if recipient.status in (
                BulkMessageRecipient.Status.FAILED,
                BulkMessageRecipient.Status.SKIPPED,
            ):
                return {
                    "status": "skipped",
                    "reason": "recipient_terminal",
                    "recipient_id": str(recipient_id),
                }

            if (
                not is_within_24h_window(lead=lead)
                and not campaign.template_name
            ):
                recipient.status = BulkMessageRecipient.Status.SKIPPED
                recipient.skip_reason = (
                    "Outside 24h messaging window and no template configured."
                )
                recipient.save(
                    update_fields=[
                        "status",
                        "skip_reason",
                        "updated_at",
                    ]
                )
                return {
                    "status": "skipped",
                    "reason": "outside_24h_window",
                    "recipient_id": str(recipient_id),
                }

            if recipient.message_id:
                message = (
                    WhatsAppMessage.objects.select_for_update()
                    .select_related("account")
                    .get(id=recipient.message_id)
                )
            else:
                message = queue_outbound_message(
                    organization=campaign.organization,
                    account=campaign.account,
                    to_number=lead.phone,
                    body=campaign.body,
                    lead=lead,
                )
                recipient.message = message
                recipient.skip_reason = ""
                recipient.save(
                    update_fields=[
                        "message",
                        "skip_reason",
                        "updated_at",
                    ]
                )

            if message.status in (
                WhatsAppMessage.Status.SENT,
                WhatsAppMessage.Status.DELIVERED,
                WhatsAppMessage.Status.READ,
            ):
                recipient.status = BulkMessageRecipient.Status.SENT
                recipient.skip_reason = ""
                recipient.save(
                    update_fields=[
                        "status",
                        "skip_reason",
                        "updated_at",
                    ]
                )
                return {
                    "status": "skipped",
                    "reason": "message_already_sent",
                    "recipient_id": str(recipient_id),
                    "message_id": str(message.id),
                }

            if message.status == _WHATSAPP_SENDING_STATUS:
                return {
                    "status": "skipped",
                    "reason": "already_in_flight",
                    "recipient_id": str(recipient_id),
                    "message_id": str(message.id),
                }

            if message.status == WhatsAppMessage.Status.FAILED:
                recipient.status = BulkMessageRecipient.Status.FAILED
                recipient.skip_reason = (
                    message.error or "Attached WhatsApp message already failed."
                )[:200]
                recipient.save(
                    update_fields=[
                        "status",
                        "skip_reason",
                        "updated_at",
                    ]
                )
                return {
                    "status": "failed",
                    "reason": "message_already_failed",
                    "recipient_id": str(recipient_id),
                    "message_id": str(message.id),
                }

            if message.status != WhatsAppMessage.Status.QUEUED:
                recipient.status = BulkMessageRecipient.Status.FAILED
                recipient.skip_reason = (
                    f"Attached WhatsApp message has non-sendable status: "
                    f"{message.status}"
                )[:200]
                recipient.save(
                    update_fields=[
                        "status",
                        "skip_reason",
                        "updated_at",
                    ]
                )
                return {
                    "status": "failed",
                    "reason": "message_not_sendable",
                    "recipient_id": str(recipient_id),
                    "message_id": str(message.id),
                }

            message.status = _WHATSAPP_SENDING_STATUS
            message.error = ""
            message.save(
                update_fields=[
                    "status",
                    "error",
                    "updated_at",
                ]
            )

    except BulkMessageRecipient.DoesNotExist:
        logger.warning(
            "send_bulk_recipient_task: recipient %s not found",
            recipient_id,
        )
        return

    # Provider I/O must happen after the recipient/message claim commits.
    try:
        send_outbound_message(message=message)

    except WhatsAppSendError as exc:
        original = exc.__cause__

        # An explicit Meta 5xx response can be retried. Requeue only the same
        # durable message attached to this recipient; never create a replacement.
        if (
            isinstance(original, WhatsAppAPIError)
            and original.status_code is not None
            and original.status_code >= 500
        ):
            _requeue_whatsapp_message_after_transient_failure(
                message_id=message.id
            )
            logger.warning(
                "send_bulk_recipient_task: Meta 5xx for recipient %s; "
                "retrying the same message %s",
                recipient_id,
                message.id,
            )
            raise self.retry(exc=exc)

        if (
            isinstance(original, WhatsAppAPIError)
            and original.status_code is None
        ):
            failure_reason = (
                "Delivery outcome unknown after WhatsApp network failure; "
                "not retried automatically to avoid a duplicate bulk send."
            )
            failure_code = "delivery_outcome_unknown"
        else:
            failure_reason = str(exc)
            failure_code = "permanent_send_failure"

        _persist_whatsapp_message_failure(
            message_id=message.id,
            error=failure_reason,
        )
        _mark_bulk_recipient_failed(
            recipient_id=recipient_id,
            message_id=message.id,
            reason=failure_reason,
        )
        logger.error(
            "send_bulk_recipient_task: terminal failure for recipient %s "
            "message %s: %s",
            recipient_id,
            message.id,
            failure_reason,
        )
        return {
            "status": "failed",
            "reason": failure_code,
            "recipient_id": str(recipient_id),
            "message_id": str(message.id),
            "error": failure_reason,
        }

    except Exception as exc:
        _persist_whatsapp_message_failure(
            message_id=message.id,
            error=exc,
        )
        _mark_bulk_recipient_failed(
            recipient_id=recipient_id,
            message_id=message.id,
            reason=exc,
        )
        logger.exception(
            "send_bulk_recipient_task: unexpected failure for recipient %s",
            recipient_id,
        )
        raise

    with transaction.atomic():
        recipient = (
            BulkMessageRecipient.objects.select_for_update()
            .filter(id=recipient_id)
            .first()
        )
        if recipient is not None and recipient.message_id == message.id:
            recipient.status = BulkMessageRecipient.Status.SENT
            recipient.skip_reason = ""
            recipient.save(
                update_fields=[
                    "status",
                    "skip_reason",
                    "updated_at",
                ]
            )

    return {
        "status": "sent",
        "recipient_id": str(recipient_id),
        "message_id": str(message.id),
    }


@shared_task
def finalize_bulk_campaign_task(
    _results,
    campaign_id,
):
    """
    Runs once every send_bulk_recipient_task in the campaign's
    chord has finished (success or failure). Marks the campaign
    completed/failed based on how many recipients actually sent.
    """
    from apps.channels.models import BulkMessageCampaign
    from services.channels.bulk_service import mark_campaign_completed

    try:
        campaign = (
            BulkMessageCampaign.objects.get(
                id=campaign_id,
            )
        )

    except BulkMessageCampaign.DoesNotExist:
        logger.warning(
            "finalize_bulk_campaign_task: "
            "campaign %s not found",
            campaign_id,
        )
        return

    mark_campaign_completed(
        campaign=campaign
    )


@shared_task
def send_bulk_campaign_task(
    campaign_id,
):
    """
    Dispatcher: marks the campaign as sending, then fans out one
    send_bulk_recipient_task per pending recipient via a Celery
    chord, so finalize_bulk_campaign_task runs automatically once
    every recipient has been processed.
    """
    from apps.channels.models import (
        BulkMessageCampaign,
        BulkMessageRecipient,
    )
    from services.channels.bulk_service import mark_campaign_started

    try:
        campaign = (
            BulkMessageCampaign.objects.get(
                id=campaign_id,
            )
        )

    except BulkMessageCampaign.DoesNotExist:
        logger.warning(
            "send_bulk_campaign_task: "
            "campaign %s not found",
            campaign_id,
        )
        return

    mark_campaign_started(
        campaign=campaign
    )

    recipient_ids = list(
        BulkMessageRecipient.objects.filter(
            campaign=campaign,
            status=BulkMessageRecipient.Status.PENDING,
        ).values_list(
            "id",
            flat=True,
        )
    )

    if not recipient_ids:
        finalize_bulk_campaign_task.delay(
            None,
            str(
                campaign.id
            ),
        )
        return

    chord(
        group(
            send_bulk_recipient_task.s(
                str(recipient_id)
            )
            for recipient_id in recipient_ids
        )
    )(
        finalize_bulk_campaign_task.s(
            str(campaign.id)
        )
    )
