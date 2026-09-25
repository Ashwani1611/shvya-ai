"""Isolated Django regressions; all Google responses come from a fake provider."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import PermissionDenied, ValidationError
from django.template.loader import render_to_string
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.organizations.models import Organization
from . import test_platform_google as fixtures
from .google import create_booking_event, update_booking_event
from .google_policy import (
    ORGANIZATION_ONLY, ORGANIZATION_WITH_FALLBACK,
    organization_allows_platform_fallback, save_google_mode,
    settings_allow_platform_fallback,
)
from .models import CalendarBooking, CalendarPage, GoogleCalendarConnection
from .platform_google import (
    PlatformGoogleConnection, connection_for_booking, is_platform_booking,
)
from .templatetags.calendar_google import booking_google_organiser


class GooglePreferenceValueTests(SimpleTestCase):
    def test_only_explicit_json_boolean_true_authorises_platform(self):
        for value in (None, False, 0, 1, "true", "false", "yes", [], {}, ""):
            with self.subTest(value=value):
                self.assertFalse(settings_allow_platform_fallback({
                    "calendar_google": {"allow_platform_fallback": value},
                }))
        self.assertTrue(settings_allow_platform_fallback({
            "calendar_google": {"allow_platform_fallback": True},
        }))

    def test_absent_or_malformed_policy_is_not_consent(self):
        for settings in (None, [], "invalid", {}, {"calendar_google": True},
                         {"calendar_google": "true"}, {"calendar_google": []}):
            with self.subTest(settings=settings):
                self.assertFalse(settings_allow_platform_fallback(settings))


@override_settings(**fixtures.PLATFORM)
class GoogleOrganisationPolicyTests(TestCase):
    lead = fixtures.PlatformGoogleBookingTests.lead
    slot = fixtures.PlatformGoogleBookingTests.slot
    manual = fixtures.PlatformGoogleBookingTests.manual
    booking = fixtures.PlatformGoogleBookingTests.booking
    provider = fixtures.PlatformGoogleBookingTests.provider

    def setUp(self):
        fixtures.PlatformGoogleBookingTests.setUp(self)
        # Most tests start as a tenant that has never opted in. Existing
        # platform regressions retain their explicit opt-in fixture separately.
        self.organization.settings = {}
        self.organization.save(update_fields=["settings"])

    def authenticate(self, user=None, client=None):
        session = SessionStore()
        set_authenticated_user(session, user or self.user)
        session.save()
        (client or self.client).cookies["shvya_crm_sessionid"] = session.session_key

    def opt_in(self):
        return save_google_mode(actor=self.user, mode=ORGANIZATION_WITH_FALLBACK)

    def opt_out(self):
        return save_google_mode(actor=self.user, mode=ORGANIZATION_ONLY)

    def other_admin(self):
        other = Organization.objects.create(name="Private Clinic", package="enterprise")
        user = User.objects.create_user(
            organization=other, email="clinic-admin@example.com",
            name="Clinic Admin", role=User.Role.ADMIN,
        )
        return other, user

    def test_default_does_not_use_shared_google_credentials(self):
        booking = create_booking_event(self.booking())
        self.assertFalse(organization_allows_platform_fallback(self.organization.pk))
        self.assertFalse(booking.google_event_id)
        self.assertFalse(booking.meeting_link)
        self.assertEqual(booking.calendar_sync_status, "not_connected")
        self.request.assert_not_called()
        booking.lead.refresh_from_db()
        self.assertTrue(booking.lead.attributes["booked_at"])

    def test_opt_in_allows_fallback_for_an_unconnected_host(self):
        self.opt_in()
        booking = create_booking_event(self.booking())
        self.assertTrue(is_platform_booking(booking))
        self.assertEqual(booking.calendar_sync_status, "synced")
        self.assertTrue(booking.meeting_link)

    def test_own_account_is_primary_even_when_fallback_is_enabled(self):
        self.opt_in()
        booking = self.booking()
        connection = GoogleCalendarConnection.objects.create(
            organization=self.organization, user=self.user,
            email="tenant-owner@example.com", is_active=True,
        )
        self.assertEqual(connection_for_booking(booking), connection)
        create_booking_event(booking)
        self.assertFalse(is_platform_booking(booking))
        self.assertEqual(booking.google_calendar_id, "tenant-owner@example.com")

    def test_own_account_works_without_platform_opt_in(self):
        booking = self.booking()
        connection = GoogleCalendarConnection.objects.create(
            organization=self.organization, user=self.user,
            email="tenant-owner@example.com", is_active=True,
        )
        self.assertEqual(connection_for_booking(booking), connection)
        create_booking_event(booking)
        self.assertFalse(is_platform_booking(booking))
        self.assertTrue(booking.meeting_link)

    def test_other_organisations_consent_does_not_enable_this_one(self):
        other, other_user = self.other_admin()
        save_google_mode(actor=other_user, mode=ORGANIZATION_WITH_FALLBACK)
        self.assertTrue(organization_allows_platform_fallback(other.pk))
        self.assertIsNone(connection_for_booking(self.booking()))
        self.request.assert_not_called()

    def test_other_organisations_google_account_is_never_selected(self):
        other, other_user = self.other_admin()
        GoogleCalendarConnection.objects.create(
            organization=other, user=other_user, email="private-clinic@example.com",
        )
        self.assertIsNone(connection_for_booking(self.booking()))
        self.request.assert_not_called()

    def test_opt_out_uses_fresh_policy_not_worker_cached_organization(self):
        self.opt_in()
        booking = self.booking()
        booking.organization.refresh_from_db()
        self.assertTrue(settings_allow_platform_fallback(booking.organization.settings))
        self.opt_out()
        self.assertIsNone(connection_for_booking(booking))

    def test_existing_platform_event_remains_manageable_after_opt_out(self):
        self.opt_in()
        booking = create_booking_event(self.booking())
        original_id, original_link = booking.google_event_id, booking.meeting_link
        self.opt_out()
        self.manual(booking.lead, self.slot())
        booking.refresh_from_db()
        update_booking_event(booking)
        self.assertEqual(booking.google_event_id, original_id)
        self.assertEqual(booking.meeting_link, original_link)
        self.assertIsInstance(connection_for_booking(booking), PlatformGoogleConnection)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(booking_google_organiser(booking), "SHVYA-managed Google Meet")
        for method, _url, kwargs in self.requests:
            if method in {"POST", "PATCH"}:
                self.assertEqual(kwargs["params"]["sendUpdates"], "none")

    def test_existing_host_event_never_migrates_to_platform(self):
        self.opt_in()
        booking = self.booking()
        booking.google_event_id = "existing-host-event"
        booking.google_calendar_id = "tenant@example.com"
        booking.save(update_fields=["google_event_id", "google_calendar_id"])
        self.assertIsNone(connection_for_booking(booking))
        self.request.assert_not_called()

    def test_disabled_organisation_cannot_select_a_new_platform_event(self):
        self.opt_in()
        booking = self.booking()
        Organization.objects.filter(pk=self.organization.pk).update(is_active=False)
        self.assertIsNone(connection_for_booking(booking))
        self.request.assert_not_called()

    @patch("apps.shvya_calendar.tasks.sync_booking_calendar.delay")
    def test_recovery_does_not_queue_an_opted_out_booking(self, delay):
        from .tasks import recover_pending_google_meet
        booking = self.booking()
        CalendarBooking.objects.filter(pk=booking.pk).update(calendar_sync_status="not_connected")
        recover_pending_google_meet()
        delay.assert_not_called()
        booking.refresh_from_db()
        self.assertEqual(booking.calendar_sync_status, "not_connected")

    @patch("apps.shvya_calendar.tasks.sync_booking_calendar.delay")
    def test_recovery_queues_an_explicitly_opted_in_booking(self, delay):
        from .tasks import recover_pending_google_meet
        self.opt_in()
        booking = self.booking()
        CalendarBooking.objects.filter(pk=booking.pk).update(calendar_sync_status="not_connected")
        recover_pending_google_meet()
        delay.assert_called_once_with(str(booking.pk))

    def test_queued_new_booking_obeys_later_opt_out(self):
        from .tasks import sync_booking_calendar
        self.opt_in()
        booking = self.booking()
        self.opt_out()
        result = sync_booking_calendar.run(str(booking.pk))
        self.assertEqual(result["status"], "not_connected")
        self.request.assert_not_called()

    def test_saving_preference_preserves_other_settings_and_audits_actor(self):
        self.organization.settings = {
            "unrelated": {"enabled": True},
            "calendar_google": {"future_preference": "preserve"},
        }
        self.organization.save(update_fields=["settings"])
        self.opt_in()
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.settings["unrelated"], {"enabled": True})
        policy = self.organization.settings["calendar_google"]
        self.assertEqual(policy["future_preference"], "preserve")
        self.assertEqual(policy["updated_by"], str(self.user.pk))
        self.assertTrue(policy["updated_at"])
        self.assertIs(policy["allow_platform_fallback"], True)
        self.request.assert_not_called()

    def test_invalid_choice_is_rejected_without_a_write(self):
        with self.assertRaises(ValidationError):
            save_google_mode(actor=self.user, mode="all_organisations")
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.settings, {})

    def test_stale_admin_role_is_revalidated(self):
        User.objects.filter(pk=self.user.pk).update(role="agent")
        with self.assertRaises(PermissionDenied):
            self.opt_in()
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.settings, {})

    def test_inactive_actor_is_rejected(self):
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.opt_in()

    def test_actor_moved_to_another_organisation_is_rejected(self):
        other, _user = self.other_admin()
        User.objects.filter(pk=self.user.pk).update(organization=other)
        with self.assertRaises(PermissionDenied):
            self.opt_in()

    def test_malformed_settings_are_not_overwritten(self):
        for malformed in ([], {"calendar_google": []}, {"calendar_google": "bad"}):
            with self.subTest(malformed=malformed):
                Organization.objects.filter(pk=self.organization.pk).update(settings=malformed)
                with self.assertRaises(ValidationError):
                    self.opt_in()
                self.organization.refresh_from_db()
                self.assertEqual(self.organization.settings, malformed)

    def test_settings_screen_only_lists_this_organisations_connections(self):
        other, other_user = self.other_admin()
        GoogleCalendarConnection.objects.create(
            organization=other, user=other_user, email="private-clinic@example.com",
        )
        GoogleCalendarConnection.objects.create(
            organization=self.organization, user=self.user, email="our-owner@example.com",
        )
        self.authenticate()
        result = self.client.get(reverse("shvya_calendar:google_settings"))
        self.assertContains(result, "our-owner@example.com")
        self.assertNotContains(result, "private-clinic@example.com")
        self.assertNotContains(result, "meetings@example.com")
        self.assertNotContains(result, "test-client-secret")
        self.assertNotContains(result, "test-refresh-token")
        self.assertEqual(result.context["form"].initial["mode"], ORGANIZATION_ONLY)

    def test_get_settings_does_not_enable_fallback(self):
        self.authenticate()
        self.client.get(reverse("shvya_calendar:google_settings"))
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.settings, {})

    def test_forged_posted_organisation_id_cannot_change_other_tenant(self):
        other, _user = self.other_admin()
        self.authenticate()
        result = self.client.post(reverse("shvya_calendar:google_settings"), {
            "mode": ORGANIZATION_WITH_FALLBACK, "organization_id": str(other.pk),
        })
        self.assertEqual(result.status_code, 302)
        self.assertTrue(organization_allows_platform_fallback(self.organization.pk))
        self.assertFalse(organization_allows_platform_fallback(other.pk))
        self.request.assert_not_called()

    def test_invalid_form_does_not_write_policy(self):
        self.authenticate()
        for data in ({}, {"mode": "anything"}):
            with self.subTest(data=data):
                result = self.client.post(reverse("shvya_calendar:google_settings"), data)
                self.assertEqual(result.status_code, 400)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.settings, {})

    def test_post_requires_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        self.authenticate(client=client)
        result = client.post(reverse("shvya_calendar:google_settings"), {
            "mode": ORGANIZATION_WITH_FALLBACK,
        })
        self.assertEqual(result.status_code, 403)
        self.assertFalse(organization_allows_platform_fallback(self.organization.pk))

    def test_other_organisation_calendar_feed_does_not_expose_this_booking(self):
        booking = self.booking()
        _other, other_user = self.other_admin()
        self.authenticate(other_user)
        day = booking.start_at.date()
        result = self.client.get(reverse("shvya_calendar:events"), {
            "start": (day - timedelta(days=1)).isoformat(),
            "end": (day + timedelta(days=2)).isoformat(),
        })
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["events"], [])

    def test_other_organisation_cannot_read_slots_or_reschedule_this_booking(self):
        booking = self.booking()
        original_start = booking.start_at
        _other, other_user = self.other_admin()
        self.authenticate(other_user)
        for name in ("booking_detail", "booking_slots"):
            with self.subTest(name=name):
                result = self.client.get(reverse(
                    "shvya_calendar:" + name, kwargs={"booking_id": booking.pk},
                ))
                self.assertEqual(result.status_code, 404)
        result = self.client.post(reverse(
            "shvya_calendar:booking_update", kwargs={"booking_id": booking.pk},
        ), {"action": "reschedule", "slot_start": original_start.isoformat()})
        self.assertEqual(result.status_code, 404)
        booking.refresh_from_db()
        self.assertEqual(booking.start_at, original_start)
        self.request.assert_not_called()

    def test_other_organisation_cannot_open_our_page_editor(self):
        _other, other_user = self.other_admin()
        self.authenticate(other_user)
        result = self.client.get(reverse(
            "shvya_calendar:editor", kwargs={"page_id": self.page.pk},
        ))
        self.assertEqual(result.status_code, 404)

    def test_editor_does_not_claim_fallback_without_tenant_consent(self):
        request = self.factory.get("/")
        request.crm_user = self.user
        html = render_to_string("shvya_calendar/partials/editor_scheduling.html", {
            "request": request, "page": self.page, "google_configured": True,
        })
        self.assertNotIn("Platform configured", html)
        self.opt_in()
        html = render_to_string("shvya_calendar/partials/editor_scheduling.html", {
            "request": request, "page": self.page, "google_configured": True,
        })
        self.assertIn("Platform configured", html)

    def test_booking_label_uses_persisted_identity(self):
        booking = self.booking()
        self.assertEqual(booking_google_organiser(booking), "Not assigned yet")
        booking.google_event_id = "legacy-host-event"
        self.assertEqual(booking_google_organiser(booking), "Organisation Google account")

    def test_non_meet_booking_does_not_use_platform_even_with_consent(self):
        self.opt_in()
        booking = self.booking()
        booking.page.meeting_location = CalendarPage.MeetingLocation.PHONE
        self.assertIsNone(connection_for_booking(booking))
        self.request.assert_not_called()
