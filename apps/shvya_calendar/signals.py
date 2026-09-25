from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.organizations.models import Organization
from .attribute_sync import ensure_booked_at, map_booking_to_lead, enqueue_booking_sync
from .models import CalendarBooking, CalendarPage
from .platform_google import platform_enabled


@receiver(post_save, sender=Organization, dispatch_uid="calendar_default_booked_at")
def organization_booked_at(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        ensure_booked_at(instance.pk)


@receiver(post_save, sender=CalendarBooking, dispatch_uid="calendar_map_booked_at")
def booking_attribute(sender, instance, raw=False, update_fields=None, created=False, **kwargs):
    if raw or (update_fields is not None and not {"start_at", "status"}.intersection(update_fields)):
        return
    map_booking_to_lead(instance)
    if (created and not instance.host_id and platform_enabled()
            and instance.page.meeting_location == CalendarPage.MeetingLocation.GOOGLE_MEET):
        instance.calendar_sync_status = CalendarBooking.SyncStatus.PENDING
        CalendarBooking.objects.filter(pk=instance.pk).update(
            calendar_sync_status=CalendarBooking.SyncStatus.PENDING,
        )
        transaction.on_commit(lambda pk=instance.pk: enqueue_booking_sync(pk))
