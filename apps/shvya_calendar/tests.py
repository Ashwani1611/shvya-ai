from datetime import timedelta

from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
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
        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key
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

    def _scheduling_payload(self, **overrides):
        payload = {
            "section": "scheduling",
            "timezone": "Asia/Kolkata",
            "session_title": "Updated consultation",
            "session_description": "Updated calendar description",
            "discussion_points": "Review\nNext steps",
            "mon_enabled": "on",
            "mon_start": "09:00",
            "mon_end": "18:00",
            "tue_enabled": "on",
            "tue_start": "09:00",
            "tue_end": "18:00",
            "wed_enabled": "on",
            "wed_start": "09:00",
            "wed_end": "18:00",
            "thu_enabled": "on",
            "thu_start": "09:00",
            "thu_end": "18:00",
            "fri_enabled": "on",
            "fri_start": "09:00",
            "fri_end": "18:00",
            "sat_start": "09:00",
            "sat_end": "18:00",
            "sun_start": "09:00",
            "sun_end": "18:00",
            "bookable_days": "30",
            "minimum_notice_minutes": "60",
            "slot_duration_minutes": "30",
            "max_slots_per_day": "25",
            "bookings_per_slot": "1",
            "buffer_before_minutes": "0",
            "buffer_after_minutes": "0",
            "meeting_location": "phone",
            "invite_lead_to_event": "on",
        }
        payload.update(overrides)
        return payload

    def test_booking_page_status_publish_validation_never_returns_500(self):
        self.page.status = CalendarPage.Status.DISABLED
        self.page.meeting_location = CalendarPage.MeetingLocation.GOOGLE_MEET
        self.page.save(update_fields=["status", "meeting_location", "updated_at"])

        with patch(
            "apps.shvya_calendar.google.google_is_configured",
            return_value=False,
        ):
            response = self.client.post(
                reverse("shvya_calendar:status", args=[self.page.id]),
                {"action": "publish"},
            )

        self.assertEqual(response.status_code, 302)
        self.page.refresh_from_db()
        self.assertEqual(self.page.status, CalendarPage.Status.DISABLED)

    def test_scheduling_settings_save_and_republish_without_nested_form_failure(self):
        response = self.client.post(
            reverse("shvya_calendar:save", args=[self.page.id]),
            self._scheduling_payload(),
        )
        self.assertEqual(response.status_code, 302)
        self.page.refresh_from_db()
        self.assertEqual(self.page.session_title, "Updated consultation")
        self.assertEqual(self.page.status, CalendarPage.Status.PUBLISHED)
        self.assertGreaterEqual(self.page.current_version, 2)

    def test_scheduling_save_is_kept_but_page_disables_if_google_not_ready(self):
        with patch(
            "apps.shvya_calendar.google.google_is_configured",
            return_value=False,
        ):
            response = self.client.post(
                reverse("shvya_calendar:save", args=[self.page.id]),
                self._scheduling_payload(meeting_location="google_meet"),
            )
        self.assertEqual(response.status_code, 302)
        self.page.refresh_from_db()
        self.assertEqual(self.page.session_title, "Updated consultation")
        self.assertEqual(self.page.meeting_location, "google_meet")
        self.assertEqual(self.page.status, CalendarPage.Status.DISABLED)

    def test_unpublished_public_link_is_friendly_not_generic_404(self):
        self.page.status = CalendarPage.Status.DISABLED
        self.page.save(update_fields=["status", "updated_at"])
        response = self.client.get(
            reverse(
                "shvya_calendar_public:page",
                kwargs={"public_id": self.page.public_id, "slug": self.page.slug},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "isn’t live yet")

    def test_public_link_survives_slug_edit(self):
        old_slug = self.page.slug
        self.page.slug = "renamed-demo"
        self.page.save(update_fields=["slug", "updated_at"])

        response = self.client.get(
            reverse(
                "shvya_calendar_public:page",
                kwargs={"public_id": self.page.public_id, "slug": old_slug},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.page.intro_title)

    def test_editor_scheduling_controls_do_not_nest_action_forms(self):
        response = self.client.get(
            f"{reverse('shvya_calendar:editor', args=[self.page.id])}?tab=scheduling"
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        scheduling_start = html.index('data-tab-panel="scheduling"')
        confirmation_start = html.index('data-tab-panel="confirmation"')
        scheduling_html = html[scheduling_start:confirmation_start]

        # Delete/disconnect forms live outside the scheduling settings form and
        # are targeted by the button form= attribute. Invalid nested forms were
        # causing browsers to terminate the settings form before Save Scheduling.
        outer_start = scheduling_html.index('<form method="post" action="')
        outer_end = scheduling_html.index("</form>", outer_start)
        outer_form = scheduling_html[outer_start:outer_end]
        self.assertNotIn("google/disconnect", outer_form)
        self.assertNotIn("/blocks/", outer_form)

    def test_second_reminder_gets_next_display_order_without_integrity_error(self):
        add_url = reverse("shvya_calendar:reminder_add", args=[self.page.id])
        first = self.client.post(
            add_url,
            {
                "channel": "email",
                "name": "First reminder",
                "timing": "immediate",
                "subject": "First",
                "body": "Hello",
            },
        )
        second = self.client.post(
            add_url,
            {
                "channel": "call_reminder",
                "name": "Second reminder",
                "timing": "before",
                "timing_amount": "30",
                "timing_unit": "minutes",
                "body": "Call the lead",
            },
        )
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        orders = list(
            self.page.reminder_sequence.steps.order_by("display_order")
            .values_list("display_order", flat=True)
        )
        self.assertEqual(orders, [0, 1])

    def test_google_callback_without_saved_state_redirects_instead_of_500(self):
        response = self.client.get(reverse("shvya_calendar:google_callback"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("shvya_calendar:index"))

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
