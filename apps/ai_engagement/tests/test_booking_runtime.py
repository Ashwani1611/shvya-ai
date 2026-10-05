from datetime import datetime
from types import SimpleNamespace
import uuid

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.ai_engagement.services.booking_runtime import (
    BOOKING_STATE_KEY,
    apply_booking_plan,
    booking_capability_available,
    prepare_booking_turn,
)
from apps.crm.models import Lead, Pipeline, Stage
from apps.crm.models.reminder import LeadReminder
from apps.organizations.models import Organization
from apps.shvya_calendar.models import CalendarBooking, CalendarPage
from apps.shvya_calendar.services import publish_page
from services.crm.reminder_notification_service import due_reminder_notifications


class AICalendarBookingRuntimeTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="AI Booking Org",
            package="enterprise",
        )
        self.user = User.objects.create_user(
            email="booking-runtime@example.com",
            organization=self.organization,
            password="test-pass",
            name="Booking Admin",
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Sales",
            owner=self.user,
        )
        self.stage = Stage.objects.create(
            pipeline=self.pipeline,
            name="New Lead",
            display_order=0,
        )
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Demo Lead",
            phone="+919876543210",
            email="demo@example.com",
            lead_source="whatsapp",
        )
        all_day = {
            day: {"enabled": True, "start": "00:00", "end": "23:30"}
            for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        }
        self.page = CalendarPage.objects.create(
            organization=self.organization,
            created_by=self.user,
            updated_by=self.user,
            host=self.user,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Product Demo",
            slug="product-demo",
            page_type=CalendarPage.PageType.BOOKING,
            meeting_location=CalendarPage.MeetingLocation.PHONE,
            availability=all_day,
            minimum_notice_minutes=0,
            slot_duration_minutes=30,
            bookable_days=7,
            session_title="SHVYA Product Demo",
        )

    def message(self, body):
        return SimpleNamespace(id=uuid.uuid4(), body=body)

    def test_capability_requires_published_booking_page(self):
        self.assertFalse(booking_capability_available(self.organization))

        publish_page(page=self.page, actor=self.user)
        self.page.refresh_from_db()

        self.assertTrue(booking_capability_available(self.organization))

    def test_demo_request_offers_live_slots_then_books_selected_slot(self):
        publish_page(page=self.page, actor=self.user)
        self.page.refresh_from_db()

        request = self.message("Book a demo for further details and onboarding")
        plan = prepare_booking_turn(
            organization=self.organization,
            lead=self.lead,
            source_message=request,
        )

        self.assertTrue(plan.handled)
        self.assertEqual(plan.mode, "offer")
        self.assertGreaterEqual(len(plan.offered_slots), 1)

        offered = apply_booking_plan(
            organization=self.organization,
            lead=self.lead,
            source_message=request,
            plan=plan,
        )

        self.assertEqual(offered.status, "awaiting_selection")
        self.assertIn("Reply with 1", offered.message)
        reminder = LeadReminder.objects.get(lead=self.lead)
        self.assertEqual(reminder.status, "pending")
        self.assertLessEqual(reminder.due_at, timezone.now())
        self.assertIn(
            reminder.pk,
            {item.pk for item in due_reminder_notifications(user=self.user)},
        )

        self.lead.refresh_from_db()
        state = self.lead.attributes[BOOKING_STATE_KEY]
        self.assertEqual(state["status"], "awaiting_selection")
        self.assertEqual(state["offered_slots"], list(plan.offered_slots))

        selection = self.message("1")
        selection_plan = prepare_booking_turn(
            organization=self.organization,
            lead=self.lead,
            source_message=selection,
        )
        self.assertEqual(selection_plan.mode, "book")
        self.assertEqual(selection_plan.requested_slot, plan.offered_slots[0])

        booked = apply_booking_plan(
            organization=self.organization,
            lead=self.lead,
            source_message=selection,
            plan=selection_plan,
        )

        self.assertEqual(booked.status, "booked")
        self.assertIn("Your demo is booked for", booked.message)
        self.assertEqual(CalendarBooking.objects.filter(lead=self.lead).count(), 1)

        booking = CalendarBooking.objects.get(lead=self.lead)
        reminder.refresh_from_db()
        self.assertEqual(reminder.status, "pending")
        self.assertLessEqual(reminder.due_at, booking.start_at)
        self.assertGreaterEqual(reminder.due_at, timezone.now())

        self.lead.refresh_from_db()
        self.assertEqual(
            self.lead.attributes[BOOKING_STATE_KEY]["booking_id"],
            str(booking.pk),
        )

        repeat = self.message("Book a demo")
        repeat_plan = prepare_booking_turn(
            organization=self.organization,
            lead=self.lead,
            source_message=repeat,
        )
        self.assertEqual(repeat_plan.mode, "existing")

        repeated = apply_booking_plan(
            organization=self.organization,
            lead=self.lead,
            source_message=repeat,
            plan=repeat_plan,
        )
        self.assertEqual(repeated.status, "booked")
        self.assertIn("already booked", repeated.message)
        self.assertEqual(CalendarBooking.objects.filter(lead=self.lead).count(), 1)

    def test_available_time_requires_confirmation_before_booking(self):
        publish_page(page=self.page, actor=self.user)
        self.page.refresh_from_db()

        first = self.message("Book a demo")
        offer_plan = prepare_booking_turn(
            organization=self.organization,
            lead=self.lead,
            source_message=first,
        )
        self.assertTrue(offer_plan.offered_slots)

        slot = datetime.fromisoformat(offer_plan.offered_slots[0])
        local = slot.astimezone(timezone.get_fixed_timezone(330))
        question = self.message(
            f"Is {local.strftime('%d %b %Y at %I:%M %p')} available?"
        )
        availability_plan = prepare_booking_turn(
            organization=self.organization,
            lead=self.lead,
            source_message=question,
        )
        self.assertEqual(availability_plan.mode, "confirm")

        result = apply_booking_plan(
            organization=self.organization,
            lead=self.lead,
            source_message=question,
            plan=availability_plan,
        )
        self.assertEqual(result.status, "awaiting_confirmation")
        self.assertEqual(CalendarBooking.objects.filter(lead=self.lead).count(), 0)

        self.lead.refresh_from_db()
        confirmation = self.message("confirm")
        confirm_plan = prepare_booking_turn(
            organization=self.organization,
            lead=self.lead,
            source_message=confirmation,
        )
        self.assertEqual(confirm_plan.mode, "book")

        confirmed = apply_booking_plan(
            organization=self.organization,
            lead=self.lead,
            source_message=confirmation,
            plan=confirm_plan,
        )
        self.assertEqual(confirmed.status, "booked")
        self.assertEqual(CalendarBooking.objects.filter(lead=self.lead).count(), 1)
