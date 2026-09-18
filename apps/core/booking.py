"""Booking intake: destination is server-owned, never taken from public input."""
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.models import MarketingBookingRequest
from apps.crm.models import Lead, Stage
from apps.organizations.models import Organization

BAC_ORGANIZATION_ID = "072d3f94-b148-414a-be17-44b146d0ef6a"


class BookingUnavailable(Exception):
    pass


@transaction.atomic
def save_booking(data):
    # Serialize repeated submissions, including concurrent requests for one phone.
    organization = Organization.objects.select_for_update().filter(
        pk=BAC_ORGANIZATION_ID, is_active=True
    ).first()
    if organization is None:
        raise BookingUnavailable("Booking organization is unavailable")
    stages = Stage.objects.filter(
        Q(name__iexact="New Leads") | Q(name__iexact="New Lead"),
        pipeline__organization=organization, pipeline__is_active=True, is_active=True,
    ).select_related("pipeline").order_by("pipeline__created_at", "pk")
    stage = stages.first()
    if stage is None:
        raise BookingUnavailable("Booking New Leads stage is unavailable")
    lead, created = Lead.objects.get_or_create(
        organization=organization, phone=data["phone"],
        defaults={"pipeline": stage.pipeline, "stage": stage, "name": data["name"],
                  "email": data["email"], "lead_source": "system"},
    )
    # A repeat request reuses the tenant's unique phone record and reopens intake.
    if not created:
        lead.pipeline = stage.pipeline
        lead.stage = stage
        lead.stage_entered_at = timezone.now()
    fields = ("name", "email", "phone", "company", "preferred_date", "preferred_time",
              "goal", "interest", "consent")
    booking = MarketingBookingRequest.objects.create(
        lead=lead, source_path="/book-a-call/", **{key: data.get(key, "") for key in fields}
    )
    note = (f"BAC {booking.pk} | {data['company']} | {data['preferred_date']} "
            f"{data['preferred_time']} IST | {data['email']}\n{data['goal']}")
    lead.notes = f"{lead.notes}\n\n{note}".strip()
    lead.save(update_fields=["pipeline", "stage", "stage_entered_at", "notes", "updated_at"])
    return booking
