from importlib import import_module
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.template.loader import render_to_string
from django.test import TestCase

from apps.crm.models import AttributeDefinition
from apps.organizations.models import Organization
from .attribute_sync import ensure_booked_at
from .booking_presentation import attach_booking_links_to_leads, safe_meeting_link
from .models import CalendarBooking
from . import test_workspace as fixtures


class BookingPresentationTests(TestCase):
    setUp = fixtures.CalendarWorkspaceTests.setUp
    authenticate = fixtures.CalendarWorkspaceTests.authenticate

    def test_custom_link_and_late_google_link_use_live_booking(self):
        with self.assertNumQueries(1):
            attach_booking_links_to_leads([self.lead], organization=self.org)
        self.assertEqual(self.lead.calendar_meeting_link, "https://meet.example.com/demo")
        self.booking.meeting_link = "https://meet.google.com/abc-defg-hij"
        self.booking.save(update_fields=["meeting_link"])
        attach_booking_links_to_leads([self.lead], organization=self.org)
        self.assertEqual(self.lead.calendar_meeting_link, self.booking.meeting_link)
        html = render_to_string("shvya_calendar/partials/meeting_link.html", {
            "meeting_link": self.lead.calendar_meeting_link,
        })
        self.assertIn("Copy Meet link", html)
        self.assertIn('href="https://meet.google.com/abc-defg-hij"', html)

    def test_latest_active_booking_matches_booked_at_and_cancellation_removes_link(self):
        old = self.booking
        newer = CalendarBooking.objects.create(
            organization=self.org, page=self.page, submission=self.submission,
            lead=self.lead, host=self.user, timezone=self.page.timezone,
            start_at=old.start_at, end_at=old.end_at,
            meeting_link="https://meet.example.com/new",
        )
        attach_booking_links_to_leads([self.lead], organization=self.org)
        self.assertEqual(self.lead.calendar_meeting_link, newer.meeting_link)
        for booking in (old, newer):
            booking.status = CalendarBooking.Status.CANCELLED
            booking.save(update_fields=["status", "updated_at"])
        attach_booking_links_to_leads([self.lead], organization=self.org)
        self.assertEqual(self.lead.calendar_meeting_link, "")
        self.assertEqual(render_to_string("shvya_calendar/partials/meeting_link.html", {"meeting_link": ""}).strip(), "")

    def test_foreign_organization_never_receives_link(self):
        foreign = Organization.objects.create(name="Foreign")
        attach_booking_links_to_leads([self.lead], organization=foreign)
        self.assertEqual(self.lead.calendar_meeting_link, "")

    def test_unsafe_links_are_not_clickable(self):
        for value in ["javascript:alert(1)", "data:text/html,hello", "//evil.example", "https://["]:
            self.assertEqual(safe_meeting_link(value), "")


class DefaultBookingAttributeTests(TestCase):
    def test_new_and_existing_organization_repair(self):
        org = Organization.objects.create(name="Default calendar")
        attribute = AttributeDefinition.objects.get(organization=org, key="booked_at")
        self.assertEqual(attribute.field_type, "datetime")
        AttributeDefinition.objects.filter(pk=attribute.pk).update(is_active=False, field_type="text")
        org.save()
        attribute.refresh_from_db()
        self.assertTrue(attribute.is_active)
        self.assertEqual(attribute.field_type, "datetime")
        self.assertEqual(AttributeDefinition.objects.filter(organization=org, key="booked_at").count(), 1)

    def test_repair_handles_multiple_label_collisions(self):
        org = Organization.objects.create(name="Collision calendar")
        AttributeDefinition.objects.filter(organization=org, key="booked_at").delete()
        for index, name in enumerate(["Booked at", "Booked at (Calendar)"]):
            AttributeDefinition.objects.create(organization=org, key=f"custom_{index}", name=name)
        definition = ensure_booked_at(org.pk)
        self.assertEqual(definition.name, "Booked at (Calendar 2)")
        self.assertEqual(ensure_booked_at(org.pk).pk, definition.pk)

    def test_migration_backfills_missing_and_repairs_inactive_idempotently(self):
        missing = Organization.objects.create(name="Missing calendar")
        inactive = Organization.objects.create(name="Inactive calendar")
        AttributeDefinition.objects.filter(organization=missing, key="booked_at").delete()
        AttributeDefinition.objects.filter(organization=inactive, key="booked_at").update(is_active=False, field_type="text")
        migration = import_module("apps.shvya_calendar.migrations.0005_repair_default_booked_at")
        for _ in range(2):
            migration.repair_booked_at(apps, SimpleNamespace(connection=connection))
        for org in (missing, inactive):
            definition = AttributeDefinition.objects.get(organization=org, key="booked_at")
            self.assertTrue(definition.is_active)
            self.assertEqual(definition.field_type, "datetime")
