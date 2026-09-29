from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import TestCase, override_settings

from apps.accounts.models import User

from . import test_platform_google as fixtures
from .booking_services import book_slot
from .google import GoogleCalendarError, create_booking_event, update_booking_event
from .models import CalendarSubmission, GoogleCalendarConnection
from .platform_google import check_platform_event, connection_for_booking, is_platform_booking


@override_settings(**fixtures.PLATFORM)
class PlatformGoogleEdgeTests(TestCase):
    setUp = fixtures.PlatformGoogleBookingTests.setUp
    lead = fixtures.PlatformGoogleBookingTests.lead
    slot = fixtures.PlatformGoogleBookingTests.slot
    manual = fixtures.PlatformGoogleBookingTests.manual
    booking = fixtures.PlatformGoogleBookingTests.booking
    provider = fixtures.PlatformGoogleBookingTests.provider

    def submission(self):
        return CalendarSubmission.objects.create(
            organization=self.organization, page=self.page, page_version=self.version,
            lead=self.lead(), status=CalendarSubmission.Status.LEAD_MATCHED,
        )

    def test_public_booking_without_host_gets_platform_meet(self):
        self.page.host = None
        self.page.save(update_fields=["host", "updated_at"])
        booking = book_slot(page=self.page, submission=self.submission(), slot_start_iso=self.slot().isoformat())
        self.assertTrue(is_platform_booking(booking))
        self.assertTrue(booking.meeting_link)
        self.assertEqual(booking.calendar_sync_status, "synced")

    @patch("apps.shvya_calendar.attribute_sync.enqueue_booking_sync")
    def test_public_provider_outage_keeps_booking_and_enqueues_retry(self, enqueue):
        self.request.side_effect = GoogleCalendarError("Provider temporarily unavailable", transient=True)
        with self.captureOnCommitCallbacks(execute=True):
            booking = book_slot(page=self.page, submission=self.submission(), slot_start_iso=self.slot().isoformat())
            enqueue.assert_not_called()
        enqueue.assert_called_once_with(booking.pk)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "scheduled")
        self.assertEqual(booking.calendar_sync_status, "pending")
        self.assertFalse(booking.meeting_link)
        booking.lead.refresh_from_db()
        self.assertTrue(booking.lead.attributes["booked_at"])

    def test_new_booking_prefers_its_own_host_connection(self):
        booking = self.booking()
        connection = GoogleCalendarConnection.objects.create(
            organization=self.organization, user=self.user, email="owner@example.com",
        )
        self.assertEqual(connection_for_booking(booking), connection)
        create_booking_event(booking)
        self.assertFalse(is_platform_booking(booking))
        self.assertEqual(booking.google_calendar_id, "owner@example.com")

    def test_changing_page_host_does_not_move_existing_event(self):
        booking = self.booking()
        connection = GoogleCalendarConnection.objects.create(
            organization=self.organization, user=self.user, email="owner@example.com",
        )
        create_booking_event(booking)
        other = User.objects.create_user(email="new-host@example.com", organization=self.organization, role=User.Role.ADMIN)
        GoogleCalendarConnection.objects.create(
            organization=self.organization, user=other, email="new-owner@example.com",
        )
        booking.page.host = other
        booking.page.save(update_fields=["host", "updated_at"])
        self.assertEqual(connection_for_booking(booking), connection)
        update_booking_event(booking)
        self.assertIn("owner%40example.com", self.requests[-1][1])
        self.assertNotIn("new-owner", self.requests[-1][1])

    def test_disabled_platform_does_not_create_google_event(self):
        booking = self.booking()
        with self.settings(GOOGLE_CALENDAR_PLATFORM_ENABLED=False):
            create_booking_event(booking)
        self.assertEqual(booking.calendar_sync_status, "not_connected")
        self.assertFalse(booking.google_event_id)
        self.request.assert_not_called()

    def test_malformed_ownership_metadata_fails_closed(self):
        booking = create_booking_event(self.booking())
        with self.assertRaisesMessage(GoogleCalendarError, "ownership"):
            check_platform_event(booking, {"id": booking.google_event_id, "extendedProperties": "invalid"})

    def render_scheduling(self):
        request = self.factory.get("/")
        request.crm_user = self.user
        return render_to_string("shvya_calendar/partials/editor_scheduling.html", {
            "page": self.page, "google_configured": True, "request": request,
        })

    def test_editor_shows_configured_platform_without_leaking_identity_or_secrets(self):
        html = self.render_scheduling()
        self.assertIn("Platform configured", html)
        self.assertIn("Connect your own Google Calendar", html)
        self.assertNotIn("test-refresh-token", html)
        self.assertNotIn("test-client-secret", html)
        self.assertNotIn("meetings@example.com", html)
        self.assertNotIn("selected host must sign in", html)

    def test_editor_does_not_claim_connected_when_backend_setup_missing(self):
        with self.settings(GOOGLE_CALENDAR_PLATFORM_REFRESH_TOKEN=""):
            html = self.render_scheduling()
        self.assertIn("needs backend setup", html)
        self.assertNotIn("Platform configured", html)
