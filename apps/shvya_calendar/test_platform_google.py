from copy import deepcopy
from datetime import timedelta
from io import StringIO
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from . import test_attribute_sync as fixtures
from .booking_services import book_slot
from .google import (
    GoogleCalendarError, cancel_booking_event, create_booking_event,
    free_busy, update_booking_event,
)
from .models import CalendarBooking, CalendarPage, CalendarSubmission
from .platform_google import (
    PlatformGoogleConnection, _token_cache, connection_for_booking,
    platform_access_token, platform_event_id, platform_status,
)

PLATFORM = {
    "GOOGLE_CALENDAR_PLATFORM_ENABLED": True,
    "GOOGLE_CALENDAR_CLIENT_ID": "test-client-id",
    "GOOGLE_CALENDAR_CLIENT_SECRET": "test-client-secret",
    "GOOGLE_CALENDAR_PLATFORM_REFRESH_TOKEN": "test-refresh-token",
    "GOOGLE_CALENDAR_PLATFORM_ACCOUNT_EMAIL": "meetings@example.com",
    "GOOGLE_CALENDAR_PLATFORM_CALENDAR_ID": "meetings@example.com",
}


def response(payload, status=200):
    result = Mock(ok=200 <= status < 300, status_code=status, headers={})
    result.json.return_value = deepcopy(payload)
    return result


@override_settings(**PLATFORM)
class PlatformGoogleBookingTests(TestCase):
    lead = fixtures.CalendarAttributeSyncTests.lead
    slot = fixtures.CalendarAttributeSyncTests.slot
    manual = fixtures.CalendarAttributeSyncTests.manual

    def setUp(self):
        fixtures.CalendarAttributeSyncTests.setUp(self)
        self.page.meeting_location = CalendarPage.MeetingLocation.GOOGLE_MEET
        self.page.invite_lead_to_event = False
        self.page.save()
        self.events = {}
        self.requests = []
        headers = patch("apps.shvya_calendar.google._headers", return_value={})
        admission = patch("apps.shvya_calendar.google._admit_google_request")
        provider = patch("apps.shvya_calendar.google._google_request", side_effect=self.provider)
        headers.start()
        admission.start()
        self.request = provider.start()
        self.addCleanup(headers.stop)
        self.addCleanup(admission.stop)
        self.addCleanup(provider.stop)

    def provider(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        event_id = url.rsplit("/", 1)[-1]
        if method == "POST" and url.endswith("/events"):
            event = deepcopy(kwargs["json"])
            event_id = event["id"]
            if event_id in self.events:
                return response({}, 409)
            event["hangoutLink"] = "https://meet.google.com/" + event_id[-10:]
            event["htmlLink"] = "https://calendar.google.com/event?eid=" + event_id
            self.events[event_id] = event
            return response(event)
        if event_id not in self.events:
            return response({}, 404)
        if method == "PATCH":
            self.events[event_id].update(deepcopy(kwargs["json"]))
        if method == "DELETE":
            del self.events[event_id]
            return response({}, 204)
        return response(self.events[event_id])

    def booking(self):
        return self.manual(self.lead(), self.slot())

    def test_manual_attribute_creates_calendar_booking_and_platform_meet(self):
        booking = self.booking()
        from .tasks import sync_booking_calendar
        self.assertEqual(sync_booking_calendar.run(str(booking.pk))["status"], "synced")
        booking.refresh_from_db()
        self.assertEqual(booking.google_event_id, platform_event_id(booking))
        self.assertTrue(booking.meeting_link.startswith("https://meet.google.com/"))
        self.assertEqual(booking.google_calendar_id, "meetings@example.com")
        booking.lead.refresh_from_db()
        self.assertTrue(booking.lead.attributes["booked_at"])

    def test_public_calendar_booking_maps_back_to_crm(self):
        lead = self.lead()
        submission = CalendarSubmission.objects.create(
            organization=self.organization, page=self.page, page_version=self.version,
            lead=lead, status=CalendarSubmission.Status.LEAD_MATCHED,
        )
        booking = book_slot(page=self.page, submission=submission, slot_start_iso=self.slot().isoformat())
        lead.refresh_from_db()
        self.assertTrue(lead.attributes["booked_at"])
        self.assertTrue(booking.meeting_link)
        self.assertEqual(len(self.events), 1)

    def test_reschedule_preserves_event_and_link_without_notifications(self):
        booking = create_booking_event(self.booking())
        event_id, link = booking.google_event_id, booking.meeting_link
        lead = booking.lead
        self.manual(lead, self.slot())
        booking.refresh_from_db()
        update_booking_event(booking)
        self.assertEqual(booking.google_event_id, event_id)
        self.assertEqual(booking.meeting_link, link)
        self.assertEqual(self.events[event_id]["start"]["dateTime"], booking.start_at.isoformat())
        for method, _url, kwargs in self.requests:
            if method in {"POST", "PATCH"}:
                self.assertEqual(kwargs["params"]["sendUpdates"], "none")
        self.assertEqual(len(self.events), 1)

    def test_unique_links_and_retry_safe_ids(self):
        first = create_booking_event(self.booking())
        second = create_booking_event(self.manual(self.lead("+919876543212"), self.slot()))
        self.assertNotEqual(first.meeting_link, second.meeting_link)
        self.assertNotEqual(first.google_event_id, second.google_event_id)
        create_booking_event(first)
        self.assertEqual(len(self.events), 2)
        self.assertEqual(first.calendar_sync_status, "synced")

    def test_shared_events_are_private_and_have_scoped_metadata(self):
        booking = create_booking_event(self.booking())
        event = self.events[booking.google_event_id]
        self.assertEqual(event["visibility"], "private")
        self.assertEqual(event["transparency"], "transparent")
        self.assertFalse(event["guestsCanSeeOtherGuests"])
        self.assertFalse(event["guestsCanInviteOthers"])
        self.assertFalse(event["reminders"]["useDefault"])
        self.assertNotIn("attendees", event)
        self.assertEqual(event["extendedProperties"]["private"]["shvya_organization_id"], str(self.organization.pk))

    def test_existing_platform_booking_does_not_switch_when_host_connects(self):
        booking = create_booking_event(self.booking())
        with patch("apps.shvya_calendar.google.connection_for_page") as host:
            self.assertIsInstance(connection_for_booking(booking), PlatformGoogleConnection)
            update_booking_event(booking)
            host.assert_not_called()

    def test_existing_host_booking_never_uses_platform_credentials(self):
        booking = self.booking()
        booking.google_event_id = "legacy-host-event"
        booking.google_calendar_id = "primary"
        booking.save(update_fields=["google_event_id", "google_calendar_id"])
        self.assertIsNone(connection_for_booking(booking))
        update_booking_event(booking)
        self.assertEqual(booking.calendar_sync_status, "not_connected")
        self.request.assert_not_called()

    def test_platform_calendar_change_does_not_move_old_event(self):
        booking = create_booking_event(self.booking())
        with self.settings(GOOGLE_CALENDAR_PLATFORM_CALENDAR_ID="different@example.com"):
            with self.assertRaisesMessage(GoogleCalendarError, "previous SHVYA Google calendar"):
                update_booking_event(booking)
        self.assertEqual(len(self.events), 1)

    def test_google_outage_recovers_persisted_identity(self):
        booking = self.booking()
        self.request.side_effect = GoogleCalendarError("Temporary provider outage", transient=True)
        with self.assertRaises(GoogleCalendarError):
            create_booking_event(booking)
        booking.refresh_from_db()
        self.assertEqual(booking.google_event_id, platform_event_id(booking))
        self.request.side_effect = self.provider
        update_booking_event(booking)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(booking.calendar_sync_status, "synced")

    def test_platform_busy_calendar_does_not_block_tenant_slots(self):
        start = self.slot()
        self.assertEqual(free_busy(page=self.page, time_min=start, time_max=start + timedelta(hours=1)), [])
        self.request.assert_not_called()

    def test_wrong_tenant_event_is_not_attached(self):
        booking = create_booking_event(self.booking())
        self.events[booking.google_event_id]["extendedProperties"]["private"]["shvya_organization_id"] = str(uuid4())
        with self.assertRaisesMessage(GoogleCalendarError, "ownership"):
            create_booking_event(booking)

    def test_cancellation_removes_only_its_event_without_notifications(self):
        booking = create_booking_event(self.booking())
        self.events["unrelated"] = {"id": "unrelated"}
        cancel_booking_event(booking)
        self.assertEqual(set(self.events), {"unrelated"})
        self.assertEqual(self.requests[-1][2]["params"]["sendUpdates"], "none")

    def test_missing_backend_credentials_do_not_invent_meet_link(self):
        booking = self.booking()
        with self.settings(GOOGLE_CALENDAR_PLATFORM_REFRESH_TOKEN=""):
            with self.assertRaisesMessage(GoogleCalendarError, "backend configuration"):
                create_booking_event(booking)
        self.assertFalse(booking.meeting_link)
        self.assertFalse(booking.google_event_id)
        self.request.assert_not_called()

    def test_non_meet_booking_never_uses_platform_account(self):
        booking = self.booking()
        booking.page.meeting_location = CalendarPage.MeetingLocation.PHONE
        self.assertIsNone(connection_for_booking(booking))

    @patch("apps.shvya_calendar.tasks.sync_booking_calendar.delay")
    def test_enabling_platform_credentials_recovers_unconnected_bookings(self, delay):
        booking = self.booking()
        CalendarBooking.objects.filter(pk=booking.pk).update(calendar_sync_status="not_connected")
        from .tasks import recover_pending_google_meet
        recover_pending_google_meet()
        delay.assert_called_once_with(str(booking.pk))


@override_settings(**PLATFORM)
class PlatformCredentialTests(SimpleTestCase):
    def setUp(self):
        _token_cache.clear()
        self.addCleanup(_token_cache.clear)

    def test_configuration_status_never_exposes_credentials(self):
        status = platform_status()
        self.assertTrue(status["configured"])
        self.assertNotIn("test-refresh-token", str(status))
        self.assertNotIn("test-client-secret", str(status))

    @patch("apps.shvya_calendar.google._admit_google_request")
    @patch("apps.shvya_calendar.google._google_request")
    def test_refresh_validates_owner_and_caches_token(self, request, _admit):
        request.side_effect = [response({"access_token": "access-value", "expires_in": 3600}),
                               response({"email": "meetings@example.com", "verified_email": True})]
        connection = PlatformGoogleConnection(uuid4(), "meetings@example.com", "meetings@example.com")
        self.assertEqual(platform_access_token(connection), "access-value")
        self.assertEqual(platform_access_token(connection), "access-value")
        self.assertEqual(request.call_count, 2)
        self.assertNotIn("meetings@example.com", repr(connection))

    @patch("apps.shvya_calendar.google._admit_google_request")
    @patch("apps.shvya_calendar.google._google_request")
    def test_wrong_google_owner_is_rejected(self, request, _admit):
        request.side_effect = [response({"access_token": "access-value"}),
                               response({"email": "wrong@example.com", "verified_email": True})]
        connection = PlatformGoogleConnection(uuid4(), "meetings@example.com", "meetings@example.com")
        with self.assertRaisesMessage(GoogleCalendarError, "does not match"):
            platform_access_token(connection)
        self.assertFalse(_token_cache)

    def test_diagnostic_command_is_offline_by_default_and_redacts_secrets(self):
        output = StringIO()
        with patch("apps.shvya_calendar.google._google_request") as request:
            call_command("check_google_calendar", stdout=output)
            request.assert_not_called()
        self.assertNotIn("test-client-secret", output.getvalue())
        self.assertNotIn("test-refresh-token", output.getvalue())
