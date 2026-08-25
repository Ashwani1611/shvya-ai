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
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction

from apps.channels.models import WhatsAppMessage
from apps.channels.providers.whatsapp import WhatsAppAPIError, WhatsAppClient
from apps.crm.models import Lead, Pipeline, Stage
from services.crm.lead_service import upsert_lead


class WhatsAppSendError(Exception):
    """Raised when an outbound message could not be sent."""


# ============================================================
# PIPELINE / LEAD ROUTING
# ============================================================


def resolve_pipeline(*, organization, to_number):
    """
    Route an inbound message to a Pipeline based on which
    business number it was sent to (Pipeline.phone_number).

    Falls back to a pipeline named "Leads", then to the
    organization's first active pipeline, so a new inbound
    message never fails just because no number was configured.
    """
    pipeline = Pipeline.objects.filter(
        organization=organization,
        phone_number=to_number,
        is_active=True,
    ).first()

    if pipeline:
        return pipeline

    pipeline = Pipeline.objects.filter(
        organization=organization,
        name="Leads",
        is_active=True,
    ).first()

    if pipeline:
        return pipeline

    return Pipeline.objects.filter(
        organization=organization,
        is_active=True,
    ).order_by("name").first()


def _first_stage(pipeline):
    return Stage.objects.filter(
        pipeline=pipeline,
        is_active=True,
    ).order_by("display_order").first()


# ============================================================
# INBOUND
# ============================================================


@transaction.atomic
def handle_inbound_message(*, organization, account, external_id, from_number, to_number, body, raw_payload):
    """
    Record an inbound WhatsApp message and attach it to a Lead,
    creating the Lead (and a pipeline/stage assignment) if this
    is the first time this number has messaged in.

    Idempotent on external_id -- Meta retries webhook deliveries,
    so a repeat call with the same wamid must be a no-op, not a
    duplicate row.
    """
    existing = WhatsAppMessage.objects.filter(
        external_id=external_id,
    ).first()

    if existing:
        return existing

    lead = None

    pipeline = resolve_pipeline(
        organization=organization,
        to_number=to_number,
    )

    if pipeline:
        stage = _first_stage(pipeline)

        if stage:
            try:
                lead, _created = upsert_lead(
                    organization=organization,
                    pipeline=pipeline,
                    stage=stage,
                    name=from_number,
                    phone=from_number,
                )
            except DjangoValidationError:
                # Lead already exists under a different pipeline/stage --
                # don't fight the CRM's existing assignment, just log
                # the message against whatever lead already matches
                # this phone number for this organization.
                lead = Lead.objects.filter(
                    organization=organization,
                    phone=from_number,
                ).first()

    message = WhatsAppMessage.objects.create(
        organization=organization,
        account=account,
        lead=lead,
        direction=WhatsAppMessage.Direction.INBOUND,
        external_id=external_id,
        from_number=from_number,
        to_number=to_number,
        body=body,
        status=WhatsAppMessage.Status.RECEIVED,
        raw_payload=raw_payload,
    )

    return message


def handle_status_update(*, external_id, status, raw_payload):
    """
    Update an outbound message's delivery status from a Meta
    status-callback webhook event (sent/delivered/read/failed).

    Silently no-ops if we don't have a matching message -- Meta
    may report statuses for messages sent before this system
    existed, or for read receipts on messages we didn't log.
    """
    status_map = {
        "sent": WhatsAppMessage.Status.SENT,
        "delivered": WhatsAppMessage.Status.DELIVERED,
        "read": WhatsAppMessage.Status.READ,
        "failed": WhatsAppMessage.Status.FAILED,
    }

    mapped_status = status_map.get(status)

    if not mapped_status:
        return None

    updated = WhatsAppMessage.objects.filter(
        external_id=external_id,
    ).update(
        status=mapped_status,
        raw_payload=raw_payload,
    )

    return updated


# ============================================================
# OUTBOUND
# ============================================================


def queue_outbound_message(*, organization, account, to_number, body, lead=None):
    """
    Create the WhatsAppMessage row in `queued` status. The actual
    Meta API call happens later, in a Celery task
    (apps.channels.tasks.send_whatsapp_message_task) -- never
    synchronously from a view, per CLAUDE.md rule 3.
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


def send_outbound_message(*, message: WhatsAppMessage):
    """
    Actually call Meta's API for an already-queued WhatsAppMessage.
    Called from inside the Celery task, not directly from a view.
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
        message.status = WhatsAppMessage.Status.FAILED
        message.error = str(exc)
        message.save(update_fields=["status", "error", "updated_at"])
        raise WhatsAppSendError(str(exc)) from exc

    external_id = None
    messages = response.get("messages") or []

    if messages:
        external_id = messages[0].get("id")

    message.status = WhatsAppMessage.Status.SENT
    message.external_id = external_id
    message.raw_payload = response
    message.save(
        update_fields=["status", "external_id", "raw_payload", "updated_at"]
    )

    return message