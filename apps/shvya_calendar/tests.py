from datetime import timedelta

from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.crm.models import AttributeDefinition, Lead, Pipeline, Stage
from apps.organizations.models import Organization

from .models import (
    CalendarBooking,
    CalendarPage,
    CalendarReminderSequence,
    CalendarReminderStep,
    CalendarSubmission,
)
from .services import (
    create_submission_and_lead,
    publish_page,
    schedule_booking_reminders,
    validate_public_submission,
)


class ShvyaCalendarServiceTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Calendar Org")
        self.user = User.objects.create_user(
            email="calendar@example.com",
            organization=self.organization,
            password="test-pass",
            name="Calendar Admin",
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
        self.attribute = AttributeDefinition.objects.create(
            organization=self.organization,
            key="budget",
            name="Budget",
            field_type=AttributeDefinition.FieldType.NUMERIC,
        )
        self.page = CalendarPage.objects.create(
            organization=self.organization,
            created_by=self.user,
            updated_by=self.user,
            host=self.user,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Demo",
            slug="demo",
            meeting_location=CalendarPage.MeetingLocation.PHONE,
            form_schema=[
                {
                    "key": "email",
                    "label": "Email",
                    "map_to": "email",
                    "field_type": "email",
                    "placeholder": "",
                    "required": False,
                    "options": [],
                },
                {
                    "key": "budget",
                    "label": "Budget",
                    "map_to": "attr:budget",
                    "field_type": "numeric",
                    "placeholder": "",
                    "required": False,
                    "options": [],
                },
            ],
        )
        self.version = publish_page(page=self.page, actor=self.user)
        self.page.refresh_from_db()
        self.factory = RequestFactory()

    def _request(self, data):
        request = self.factory.post("/calendar/demo/submit/", data=data)
        request.GET = request.GET.copy()
        request.META["REMOTE_ADDR"] = "203.0.113.10"
        return request

    def test_unknown_public_field_is_rejected(self):
        request = self._request(
            {
                "name": "Rahul",
                "mobile": "+919876543210",
                "consent": "on",
                "f_admin_state": "lost",
            }
        )
        with self.assertRaises(ValidationError):
            validate_public_submission(
                page=self.page,
                version=self.version,
                post=request.POST,
                files=request.FILES,
            )

    def test_submission_creates_calendar_lead_and_mapped_attributes(self):
        request = self._request(
            {
                "name": "Rahul",
                "mobile": "+919876543210",
                "consent": "on",
                "f_email": "rahul@example.com",
                "f_budget": "100000",
            }
        )
        submitted, normalized = validate_public_submission(
            page=self.page,
            version=self.version,
            post=request.POST,
            files=request.FILES,
        )
        submission, lead, created = create_submission_and_lead(
            page=self.page,
            version=self.version,
            submitted=submitted,
            normalized=normalized,
            request=request,
            files=request.FILES,
        )
        self.assertTrue(created)
        self.assertEqual(lead.lead_source, "shvya_calendar")
        self.assertEqual(lead.pipeline, self.pipeline)
        self.assertEqual(lead.stage, self.stage)
        self.assertEqual(lead.email, "rahul@example.com")
        self.assertEqual(lead.attributes["budget"], "100000")
        self.assertEqual(submission.lead, lead)
        self.assertTrue(submission.consent_accepted)

    def test_existing_lead_keeps_pipeline_stage_and_fills_blank_values(self):
        other_pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Enterprise",
            owner=self.user,
        )
        other_stage = Stage.objects.create(
            pipeline=other_pipeline,
            name="Working",
            display_order=0,
        )
        existing = Lead.objects.create(
            organization=self.organization,
            pipeline=other_pipeline,
            stage=other_stage,
            name="Existing Name",
            phone="+919999999999",
            email="",
            lead_source="system",
            attributes={},
        )
        request = self._request(
            {
                "name": "Submitted Name",
                "mobile": "+919999999999",
                "consent": "on",
                "f_email": "new@example.com",
                "f_budget": "50000",
            }
        )
        submitted, normalized = validate_public_submission(
            page=self.page,
            version=self.version,
            post=request.POST,
            files=request.FILES,
        )
        _submission, lead, created = create_submission_and_lead(
            page=self.page,
            version=self.version,
            submitted=submitted,
            normalized=normalized,
            request=request,
            files=request.FILES,
        )
        lead.refresh_from_db()
        self.assertFalse(created)
        self.assertEqual(lead.id, existing.id)
        self.assertEqual(lead.pipeline, other_pipeline)
        self.assertEqual(lead.stage, other_stage)
        self.assertEqual(lead.name, "Existing Name")
        self.assertEqual(lead.email, "new@example.com")
        self.assertEqual(lead.attributes["budget"], "50000")
        self.assertEqual(lead.lead_source, "system")

    def test_conflicting_phone_and_email_matches_are_rejected(self):
        phone_lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Phone Lead",
            phone="+917000000001",
            email="phone@example.com",
        )
        Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Email Lead",
            phone="+917000000002",
            email="shared@example.com",
        )
        request = self._request(
            {
                "name": "Conflicting Person",
                "mobile": phone_lead.phone,
                "consent": "on",
                "f_email": "shared@example.com",
            }
        )
        submitted, normalized = validate_public_submission(
            page=self.page,
            version=self.version,
            post=request.POST,
            files=request.FILES,
        )
        with self.assertRaises(ValidationError):
            create_submission_and_lead(
                page=self.page,
                version=self.version,
                submitted=submitted,
                normalized=normalized,
                request=request,
                files=request.FILES,
            )

    def test_calendar_data_is_removed_when_crm_lead_is_deleted(self):
        request = self._request(
            {
                "name": "Delete Me",
                "mobile": "+917000000003",
                "consent": "on",
            }
        )
        submitted, normalized = validate_public_submission(
            page=self.page,
            version=self.version,
            post=request.POST,
            files=request.FILES,
        )
        submission, lead, _created = create_submission_and_lead(
            page=self.page,
            version=self.version,
            submitted=submitted,
            normalized=normalized,
            request=request,
            files=request.FILES,
        )
        booking = CalendarBooking.objects.create(
            organization=self.organization,
            page=self.page,
            submission=submission,
            lead=lead,
            host=self.user,
            start_at=timezone.now() + timedelta(days=1),
            end_at=timezone.now() + timedelta(days=1, minutes=30),
            timezone="Asia/Kolkata",
        )
        submission_id = submission.id
        booking_id = booking.id

        lead.delete()

        self.assertFalse(CalendarSubmission.objects.filter(pk=submission_id).exists())
        self.assertFalse(CalendarBooking.objects.filter(pk=booking_id).exists())

    def test_cross_organization_pipeline_is_rejected(self):
        other_org = Organization.objects.create(name="Other Org")
        other_pipeline = Pipeline.objects.create(
            organization=other_org,
            name="Other",
        )
        self.page.pipeline = other_pipeline
        self.page.stage = None
        with self.assertRaises(ValidationError):
            self.page.full_clean()

    def test_reminder_channels_do_not_include_ai_call(self):
        channel_values = {
            value
            for value, _label in CalendarReminderStep.Channel.choices
        }
        self.assertEqual(
            channel_values,
            {"whatsapp", "email", "call_reminder"},
        )
        self.assertNotIn("ai_call", channel_values)

    @patch("apps.shvya_calendar.tasks.dispatch_calendar_reminder.delay")
    def test_immediate_reminder_is_queued_after_booking(self, mocked_delay):
        sequence = CalendarReminderSequence.objects.create(page=self.page)
        step = CalendarReminderStep.objects.create(
            sequence=sequence,
            channel=CalendarReminderStep.Channel.CALL_REMINDER,
            name="Call lead",
            timing_mode=CalendarReminderStep.TimingMode.IMMEDIATE,
            offset_minutes=0,
            display_order=0,
        )
        lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Booked Lead",
            phone="+918888888888",
            lead_source="shvya_calendar",
        )
        request = self._request(
            {
                "name": "Booked Lead",
                "mobile": "+918888888888",
                "consent": "on",
            }
        )
        submitted, normalized = validate_public_submission(
            page=self.page,
            version=self.version,
            post=request.POST,
            files=request.FILES,
        )
        submission, _lead, _created = create_submission_and_lead(
            page=self.page,
            version=self.version,
            submitted=submitted,
            normalized=normalized,
            request=request,
            files=request.FILES,
        )
        booking = CalendarBooking.objects.create(
            organization=self.organization,
            page=self.page,
            submission=submission,
            lead=lead,
            host=self.user,
            start_at=timezone.now() + timedelta(hours=4),
            end_at=timezone.now() + timedelta(hours=4, minutes=30),
            timezone="Asia/Kolkata",
        )
        deliveries = schedule_booking_reminders(booking)
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0].step, step)
        self.assertLessEqual(
            abs((deliveries[0].due_at - timezone.now()).total_seconds()),
            5,
        )
        mocked_delay.assert_called_once()
