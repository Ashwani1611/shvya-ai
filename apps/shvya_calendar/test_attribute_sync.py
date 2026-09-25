from datetime import timedelta
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.crm.models import AttributeDefinition, Lead
from apps.crm.templatetags.crm_extras import appointment_datetime
from apps.organizations.models import Organization
from .availability import _page_zone, available_slots
from .google import create_booking_event
from .models import CalendarBooking, CalendarPage, CalendarSubmission
from . import tests as calendar_fixtures


class CalendarAttributeSyncTests(TestCase):
    setUp = calendar_fixtures.ShvyaCalendarServiceTests.setUp

    def lead(self, phone="+919876543211"):
        return Lead.objects.create(organization=self.organization, pipeline=self.pipeline,
                                   stage=self.stage, name="Calendar Lead", phone=phone)

    def slot(self):
        day = timezone.now().astimezone(_page_zone(self.page)).date() + timedelta(days=1)
        for offset in range(7):
            slots = available_slots(page=self.page, local_date=day + timedelta(days=offset))
            if slots:
                return slots[0]["start"]
        self.fail("Fixture has no available slots")

    def manual(self, lead, start):
        lead.attributes = {"booked_at": start.isoformat(), "budget": "100"}
        lead.save(update_fields=["attributes", "updated_at"])
        return CalendarBooking.objects.get(lead=lead)

    def test_fixed_attribute_created_for_new_organization(self):
        definition = AttributeDefinition.objects.get(organization=self.organization, key="booked_at")
        self.assertEqual(definition.field_type, "datetime")

    def test_disconnected_google_does_not_block_shvya_slots(self):
        self.page.meeting_location = CalendarPage.MeetingLocation.GOOGLE_MEET
        self.page.save()
        self.assertIsNotNone(self.slot())

    def test_manual_booking_maps_and_repeated_save_is_idempotent(self):
        lead = self.lead()
        start = self.slot()
        booking = self.manual(lead, start)
        lead.refresh_from_db()
        self.assertEqual(booking.start_at, start)
        self.assertEqual(lead.attributes["budget"], "100")
        self.assertEqual(lead.attributes["booked_at"], start.astimezone(_page_zone(self.page)).strftime("%Y-%m-%dT%H:%M"))
        lead.save()
        self.assertEqual(CalendarBooking.objects.filter(lead=lead).count(), 1)
        self.assertEqual(CalendarSubmission.objects.filter(lead=lead).count(), 1)

    def test_manual_reschedule_updates_existing_booking(self):
        lead = self.lead()
        booking = self.manual(lead, self.slot())
        next_start = self.slot()
        self.manual(lead, next_start)
        booking.refresh_from_db()
        self.assertEqual(booking.start_at, next_start)
        self.assertEqual(booking.status, CalendarBooking.Status.RESCHEDULED)

    def test_cancel_clears_booked_at_and_preserves_other_attributes(self):
        lead = self.lead()
        booking = self.manual(lead, self.slot())
        booking.status = CalendarBooking.Status.CANCELLED
        booking.save(update_fields=["status", "updated_at"])
        lead.refresh_from_db()
        self.assertEqual(lead.attributes["booked_at"], "")
        self.assertEqual(lead.attributes["budget"], "100")

    def test_cannot_silently_clear_active_booking(self):
        lead = self.lead()
        self.manual(lead, self.slot())
        lead.attributes["booked_at"] = ""
        with self.assertRaises(ValidationError):
            lead.save()
        lead.refresh_from_db()
        self.assertTrue(lead.attributes["booked_at"])

    def test_unavailable_time_rolls_back_attribute(self):
        lead = self.lead()
        with self.assertRaises(ValidationError):
            self.manual(lead, timezone.now() - timedelta(days=1))
        lead.refresh_from_db()
        self.assertNotIn("booked_at", lead.attributes)
        self.assertFalse(CalendarBooking.objects.filter(lead=lead).exists())

    def test_capacity_prevents_second_manual_booking(self):
        start = self.slot()
        self.manual(self.lead(), start)
        with self.assertRaises(ValidationError):
            self.manual(self.lead("+919876543212"), start)

    def test_foreign_calendar_is_not_selected(self):
        self.page.organization = Organization.objects.create(name="Other Org")
        self.page.save(update_fields=["organization"])
        with self.assertRaises(ValidationError):
            self.manual(self.lead(), timezone.now() + timedelta(days=2))

    def test_display_uses_am_pm(self):
        self.assertEqual(appointment_datetime("2026-09-25T15:30"), "25 Sep 2026, 03:30 PM")

    @patch("apps.shvya_calendar.google._admit_google_request")
    @patch("apps.shvya_calendar.google._headers", return_value={})
    @patch("apps.shvya_calendar.google.connection_for_page")
    @patch("apps.shvya_calendar.google._google_request")
    def test_unique_meet_identity_per_booking_and_stable_retry(self, request, connection, headers, admit):
        connection.return_value = Mock(calendar_id="primary")
        request.return_value = Mock(ok=True, status_code=200)
        request.return_value.json.return_value = {"id": "event", "hangoutLink": "https://meet.google.com/abc-defg-hij"}
        self.page.meeting_location = CalendarPage.MeetingLocation.GOOGLE_MEET
        self.page.save()
        booking = self.manual(self.lead(), self.slot())
        create_booking_event(booking)
        first = request.call_args.kwargs["json"]
        create_booking_event(booking)
        second = request.call_args.kwargs["json"]
        self.assertEqual(first["id"], booking.id.hex)
        self.assertEqual(first["conferenceData"], second["conferenceData"])
        other = self.manual(self.lead("+919876543212"), self.slot())
        create_booking_event(other)
        self.assertNotEqual(first["conferenceData"], request.call_args.kwargs["json"]["conferenceData"])
        self.assertEqual(request.call_args.kwargs["params"]["conferenceDataVersion"], "1")

    @patch("apps.shvya_calendar.attribute_sync.enqueue_booking_sync")
    def test_manual_google_sync_runs_after_commit(self, enqueue):
        with self.captureOnCommitCallbacks(execute=True):
            booking = self.manual(self.lead(), self.slot())
            enqueue.assert_not_called()
        enqueue.assert_called_once_with(booking.pk)

    @patch("apps.shvya_calendar.tasks.sync_booking_calendar.delay")
    def test_recovery_includes_reschedule_with_existing_meet_link(self, delay):
        from .tasks import recover_pending_google_meet
        booking = self.manual(self.lead(), self.slot())
        booking.google_event_id = "existing"
        booking.meeting_link = "https://meet.google.com/abc-defg-hij"
        booking.calendar_sync_status = CalendarBooking.SyncStatus.PENDING
        booking.save(update_fields=["google_event_id", "meeting_link", "calendar_sync_status"])
        recover_pending_google_meet()
        delay.assert_called_once_with(str(booking.pk))

    def test_fixed_attribute_cannot_be_deleted(self):
        from services.crm.attribute_service import delete_attribute_definition
        definition = AttributeDefinition.objects.get(organization=self.organization, key="booked_at")
        with self.assertRaises(ValidationError):
            delete_attribute_definition(organization=self.organization, attribute=definition)

    @patch("apps.shvya_calendar.google.refresh_booking_event_details")
    @patch("apps.shvya_calendar.google._admit_google_request")
    @patch("apps.shvya_calendar.google._headers", return_value={})
    @patch("apps.shvya_calendar.google.connection_for_page")
    @patch("apps.shvya_calendar.google._google_request")
    def test_duplicate_google_insert_recovers_original_event(self, request, connection, headers, admit, refresh):
        connection.return_value = Mock(calendar_id="primary")
        request.return_value = Mock(ok=False, status_code=409)
        with patch("apps.shvya_calendar.availability.free_busy", return_value=[]):
            booking = self.manual(self.lead(), self.slot())
        create_booking_event(booking)
        booking.refresh_from_db()
        self.assertEqual(booking.google_event_id, booking.id.hex)
        refresh.assert_called_once_with(booking)
