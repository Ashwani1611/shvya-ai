from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.organizations.models import Organization
from .attribute_sync import ensure_booked_at, map_booking_to_lead
from .models import CalendarBooking


@receiver(post_save, sender=Organization, dispatch_uid="calendar_default_booked_at")
def organization_booked_at(sender, instance, created, raw=False, **kwargs):
    if not raw:
        ensure_booked_at(instance.pk)


@receiver(post_save, sender=CalendarBooking, dispatch_uid="calendar_map_booked_at")
def booking_attribute(sender, instance, raw=False, update_fields=None, **kwargs):
    if raw or (update_fields is not None and not {"start_at", "status"}.intersection(update_fields)):
        return
    map_booking_to_lead(instance)
