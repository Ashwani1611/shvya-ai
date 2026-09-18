"""Bounded background work. No provider calls occur inside ticket transactions."""
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .access import customer_authorized
from .fields import validate_custom
from .models import EmailDelivery, SupportSettings, Ticket, TicketStatus, WorkItem
from .notifications import CUSTOMER_EVENTS, STAFF_EVENTS, record_event
from .policy import may_autoclose, platform_staff
from .services import touch

EVENT_TEXT = {
    "created": "Your support ticket has been created.",
    "staff_reply": "Shvya-Ops has replied to your support ticket.",
    "customer_reply": "A customer has replied to a support ticket.",
    "status_changed": "The support ticket status has changed.",
    "auto_closed": "This support ticket was automatically closed after inactivity. A reply reopens it.",
    "assigned": "A support ticket assignment has changed.",
    "merged": "Related support conversations have been combined into this ticket.",
    "internal_note": "A staff member added an internal note.",
    "work_due": "A support task or reminder is due.",
}


def deliver_pending(*, limit=50):
    base = getattr(settings, "SUPPORT_PUBLIC_BASE_URL", "").rstrip("/")
    try:
        parsed = urlsplit(base)
    except ValueError:
        return {"sent": 0, "configuration_required": True}
    if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
        return {"sent": 0, "configuration_required": True}
    sent = 0
    for _ in range(min(limit, 100)):
        now = timezone.now()
        with transaction.atomic():
            delivery = (EmailDelivery.objects.select_for_update(skip_locked=True)
                .filter(Q(state="pending", available_at__lte=now) |
                        Q(state="sending", claimed_at__lt=now-timedelta(minutes=10)))
                .filter(attempts__lt=5).order_by("available_at").first())
            if not delivery:
                break
            delivery.state, delivery.claimed_at = "sending", now
            delivery.attempts += 1
            delivery.save(update_fields=["state", "claimed_at", "attempts"])
        # Resolve recipient authorization and preferences again at delivery time.
        delivery = EmailDelivery.objects.select_related("recipient__organization", "event__ticket__organization").get(pk=delivery.pk)
        user, event = delivery.recipient, delivery.event
        ticket = event.ticket
        config = SupportSettings.load()
        staff = platform_staff(user)
        from .access import visible_tickets
        allowed = ((staff and config.notify_staff and event.action in STAFF_EVENTS) or
                   (customer_authorized(user) and config.notify_customer and event.action in CUSTOMER_EVENTS))
        from .models import OrganizationSupportPolicy
        if not staff and OrganizationSupportPolicy.objects.filter(organization_id=user.organization_id,
                                                                  email_notifications=False).exists():
            allowed = False
        if not allowed or not visible_tickets(user).filter(pk=ticket.pk).exists():
            EmailDelivery.objects.filter(pk=delivery.pk).update(state="skipped")
            continue
        try:
            path = reverse("support-staff:detail" if staff else "support-client:detail", args=[ticket.pk])
            if staff and ticket.merged_into_id:
                path += "?archive=1"
            message = EmailMessage(
                subject=f"[{ticket.reference}] {ticket.subject}",
                body=(f"Hello {user.name or 'there'},\n\n{EVENT_TEXT.get(event.action, 'Your support ticket has an update.')}\n\n"
                      f"Ticket: {ticket.reference}\nStatus: {ticket.status.name}\nDepartment: Shvya-Ops\n\n"
                      f"View securely: {base}{path}\n\nSign in with your SHVYA account. Do not send passwords or API keys.\n"),
                from_email=getattr(settings, "SUPPORT_FROM_EMAIL", settings.DEFAULT_FROM_EMAIL),
                to=[user.email],
                reply_to=[getattr(settings, "SUPPORT_REPLY_TO_EMAIL", settings.DEFAULT_FROM_EMAIL)],
                headers={"Message-ID": f"<support-{delivery.pk.hex}@{parsed.hostname}>",
                         "Auto-Submitted": "auto-generated", "X-Auto-Response-Suppress": "All"},
            )
            # SMTP timeout is deliberately bounded; no attachment or private-note body leaves this path.
            from django.core.mail import get_connection
            message.connection = get_connection(timeout=20)
            if message.send(fail_silently=False) != 1:
                raise RuntimeError("NoMessageAccepted")
        except Exception as exc:
            retry = delivery.attempts < 5
            EmailDelivery.objects.filter(pk=delivery.pk).update(
                state="pending" if retry else "failed",
                available_at=timezone.now()+timedelta(seconds=min(3600, 30 * 2**delivery.attempts)),
                last_error=type(exc).__name__[:80], claimed_at=None)
        else:
            EmailDelivery.objects.filter(pk=delivery.pk).update(state="sent", sent_at=timezone.now(), last_error="")
            sent += 1
    # Expired claims exhausted before acknowledgement require operator review, not endless retry.
    EmailDelivery.objects.filter(state="sending", attempts__gte=5,
        claimed_at__lt=timezone.now()-timedelta(minutes=10)).update(state="failed", last_error="DeliveryAcknowledgementUnknown")
    return {"sent": sent, "configuration_required": False}


def maintain_tickets(*, limit=100):
    config = SupportSettings.load()
    now = timezone.now()
    closed_count = 0
    if config.auto_close_hours:
        candidates = list(Ticket.objects.filter(merged_into__isnull=True,
            last_public_activity_at__lte=now-timedelta(hours=config.auto_close_hours))
            .exclude(status__behavior__in=("in_progress", "on_hold", "closed"))
            .values_list("pk", flat=True)[:limit])
        for pk in candidates:
            with transaction.atomic():
                ticket = Ticket.objects.select_for_update().get(pk=pk)
                if ticket.merged_into_id or not may_autoclose(ticket.status.behavior,
                        ticket.last_public_activity_at, now, config.auto_close_hours):
                    continue
                try:
                    validate_custom({}, staff=True, existing=ticket.custom_values, closing=True, partial=True)
                except Exception as exc:
                    from django.core.exceptions import ValidationError
                    if isinstance(exc, ValidationError):
                        continue
                    raise
                ticket.status = TicketStatus.objects.get(key="closed", system=True)
                touch(ticket)
                record_event(ticket, None, "auto_closed")
                closed_count += 1
    due_ids = list(WorkItem.objects.filter(due_at__lte=now, completed_at__isnull=True,
                    notified_at__isnull=True).values_list("pk", flat=True)[:limit])
    for pk in due_ids:
        with transaction.atomic():
            item = WorkItem.objects.select_for_update().select_related("assigned_to", "ticket").get(pk=pk)
            if item.completed_at or item.notified_at or not platform_staff(item.assigned_to):
                continue
            record_event(item.ticket, None, "work_due", {"item": item.pk}, staff_recipient=item.assigned_to)
            item.notified_at = now
            item.save(update_fields=["notified_at"])
    return {"auto_closed": closed_count, "due_processed": len(due_ids)}
