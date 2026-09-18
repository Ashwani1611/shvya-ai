"""Event + per-recipient delivery rows are saved inside the ticket transaction."""
import logging
from django.db import transaction
from .access import staff_users
from .models import EmailDelivery, OrganizationSupportPolicy, SupportSettings, TicketEvent

logger = logging.getLogger(__name__)
CUSTOMER_EVENTS = {"created", "staff_reply", "status_changed", "auto_closed", "merged", "customer_reply"}
STAFF_EVENTS = CUSTOMER_EVENTS | {"assigned", "internal_note", "work_due"}


def wake_delivery():
    try:
        from .tasks import deliver_notifications
        deliver_notifications.delay()
    except Exception:
        # The durable outbox remains eligible for Beat/management-command recovery.
        logger.warning("Support outbox wake-up unavailable; delivery remains pending.")


def record_event(ticket, actor, action, detail=None, *, staff_recipient=None):
    event = TicketEvent.objects.create(ticket=ticket, actor=actor, action=action, detail=detail or {})
    config = SupportSettings.load()
    recipients = {}
    if config.notify_staff and action in STAFF_EVENTS:
        qs = staff_users()
        if staff_recipient is not None:
            qs = qs.filter(pk=staff_recipient.pk)
        elif config.assignee_only_notifications and ticket.assignee_id:
            qs = qs.filter(pk=ticket.assignee_id)
        for user in qs:
            recipients[user.pk] = user
    customer_enabled = not OrganizationSupportPolicy.objects.filter(
        organization_id=ticket.organization_id, email_notifications=False).exists()
    if config.notify_customer and customer_enabled and action in CUSTOMER_EVENTS and action != "customer_reply":
        if ticket.requester.is_active and ticket.organization.is_active:
            recipients[ticket.requester_id] = ticket.requester
        if config.notify_organization_admins and ticket.organization.is_active:
            for user in ticket.organization.users.filter(role="admin", is_active=True, is_superuser=False):
                recipients[user.pk] = user
    if actor and action != "created":
        recipients.pop(actor.pk, None)
    EmailDelivery.objects.bulk_create([EmailDelivery(event=event, recipient=u) for u in recipients.values()],
                                      ignore_conflicts=True)
    if recipients:
        transaction.on_commit(wake_delivery)
    return event
