"""CRM reminder notification and mutation endpoints."""

from datetime import datetime, timedelta

from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.crm.decorators import crm_login_required
from apps.crm.models import LeadReminder
from services.crm.reminder_notification_service import (
    acknowledge_reminder_notification,
    due_reminder_notifications,
    reset_reminder_notification_acknowledgements,
)
from services.crm_activity_service import record_reminder_completed

# ============================================================
# GLOBAL REMINDERS
# ============================================================


@crm_login_required
@require_GET
def reminder_notification_feed(request):
    """Return due reminder popups not yet acknowledged by this dashboard user."""
    user = request.crm_user
    reminders = due_reminder_notifications(user=user)

    return JsonResponse(
        {
            "notifications": [
                {
                    "id": str(reminder.id),
                    "title": reminder.title,
                    "description": reminder.description,
                    "lead_name": reminder.lead.name,
                    "lead_id": str(reminder.lead_id),
                    "due_at": timezone.localtime(reminder.due_at).isoformat(),
                    "is_overdue": reminder.due_at < timezone.now(),
                    "ack_url": reverse(
                        "crm-reminder-notification-ack",
                        args=[reminder.id],
                    ),
                }
                for reminder in reminders
            ],
            "pending_count": LeadReminder.objects.filter(
                lead__organization=user.organization,
                status="pending",
            ).count(),
        }
    )


@crm_login_required
@require_POST
def reminder_notification_ack(request, reminder_id):
    """Acknowledge the popup only; leave the reminder itself pending."""
    user = request.crm_user
    reminder = get_object_or_404(
        LeadReminder.objects.select_related("lead"),
        id=reminder_id,
        lead__organization=user.organization,
        status="pending",
    )
    acknowledge_reminder_notification(
        user=user,
        reminder=reminder,
    )
    return JsonResponse({"acknowledged": True})



@crm_login_required
@require_POST
def global_reminder_complete(
    request,
    reminder_id,
):
    user = request.crm_user

    reminder = get_object_or_404(
        LeadReminder,
        id=reminder_id,
        lead__organization=user.organization,
        status="pending",
    )

    reminder.status = "completed"
    reminder.completed_at = timezone.now()

    reminder.save(
        update_fields=[
            "status",
            "completed_at",
            "updated_at",
        ]
    )

    record_reminder_completed(
        lead=reminder.lead,
        actor=user,
        reminder=reminder,
    )

    return HttpResponse(
        status=204
    )

@crm_login_required
@require_POST
def global_reminder_snooze(
    request,
    reminder_id,
):
    user = request.crm_user

    reminder = get_object_or_404(
        LeadReminder,
        id=reminder_id,
        lead__organization=user.organization,
        status="pending",
    )

    reminder.due_at = (
        reminder.due_at
        + timedelta(
            minutes=30
        )
    )

    reminder.save(
        update_fields=[
            "due_at",
            "updated_at",
        ]
    )
    reset_reminder_notification_acknowledgements(
        reminder=reminder,
    )

    return HttpResponse(
        status=204
    )

@crm_login_required
@require_POST
def global_reminder_delete(
    request,
    reminder_id,
):
    user = request.crm_user

    reminder = get_object_or_404(
        LeadReminder,
        id=reminder_id,
        lead__organization=user.organization,
    )

    reminder.delete()

    return HttpResponse(
        status=204
    )

@crm_login_required
@require_GET
def global_reminder_edit_modal(
    request,
    reminder_id,
):
    user = request.crm_user

    reminder = get_object_or_404(
        LeadReminder,
        id=reminder_id,
        lead__organization=user.organization,
        status="pending",
    )

    return render(
        request,
        "crm/partials/global_reminder_edit_modal.html",
        {
            "reminder": reminder,
        },
    )

@crm_login_required
@require_POST
def global_reminder_edit_save(
    request,
    reminder_id,
):
    user = request.crm_user

    reminder = get_object_or_404(
        LeadReminder,
        id=reminder_id,
        lead__organization=user.organization,
        status="pending",
    )

    due_at_raw = request.POST.get(
        "due_at",
        "",
    ).strip()

    if not due_at_raw:

        return HttpResponse(
            "Reminder date/time is required.",
            status=400,
        )

    try:

        due_at = datetime.fromisoformat(
            due_at_raw
        )

        if timezone.is_naive(
            due_at
        ):

            due_at = timezone.make_aware(
                due_at,
                timezone.get_current_timezone(),
            )

    except ValueError:

        return HttpResponse(
            "Invalid reminder date/time.",
            status=400,
        )

    reminder.due_at = due_at

    reminder.save(
        update_fields=[
            "due_at",
            "updated_at",
        ]
    )
    reset_reminder_notification_acknowledgements(
        reminder=reminder,
    )

    return HttpResponse(
        status=204
    )                   

