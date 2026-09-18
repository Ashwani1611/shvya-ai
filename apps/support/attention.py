"""Response-required state derived from the existing, committed conversation.

This is deliberately not an unread counter. GETs never acknowledge a response,
and there is no browser-local or second persisted ticket state to reconcile.
"""
from django.db.models import OuterRef, Subquery

from .access import customer_authorized, visible_tickets
from .models import Ticket, TicketMessage


def tickets_awaiting_customer(user):
    """Return actionable tickets the current organization user may access.

    Internal notes and anonymous shared-link replies are not organization
    acknowledgements. Closed/merged sources are not actionable. Reopening a
    ticket re-evaluates its existing conversation; a new staff reply can always
    require another response. Ordering matches the actual public thread.
    """
    if not customer_authorized(user):
        return Ticket.objects.none()
    latest_reply = (
        TicketMessage.objects.filter(
            ticket_id=OuterRef("pk"), internal=False,
            author_kind__in=("staff", "customer"),
        )
        .order_by("-created_at", "-id")
        .values("author_kind")[:1]
    )
    return (
        visible_tickets(user)
        .filter(merged_into__isnull=True)
        .exclude(status__behavior="closed")
        .alias(last_response_author=Subquery(latest_reply))
        .filter(last_response_author="staff")
    )


def attention_count(user):
    """Count tickets, not messages; never accept client-supplied tenant IDs."""
    return tickets_awaiting_customer(user).count()
