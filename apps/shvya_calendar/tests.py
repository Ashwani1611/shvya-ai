from datetime import timedelta
import json
import tempfile

from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.crm.models import AttributeDefinition, Lead, Pipeline, Stage
from apps.organizations.models import Organization

from .models import (
    CalendarBlock,
    CalendarBooking,
    CalendarPage,
    CalendarReminderSequence,
    CalendarReminderStep,
    CalendarSubmission,
    CalendarSubmissionAttachment,
)
from .google import GoogleCalendarError
from .services import (
    available_slots,
    book_slot,
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
        self._media_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._media_directory.cleanup)
        self._media_override = override_settings(
            MEDIA_ROOT=self._media_directory.name
        )
        self._media_override.enable()
        self.addCleanup(self._media_override.disable)

    def _request(self, data):
        request = self.factory.post("/calendar/demo/submit/", data=data)
        request.GET = request.GET.copy()
        request.META["REMOTE_ADDR"] = "203.0.113.10"
        return request

    def _authenticate_dashboard_client(self):
        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    def test_publish_recovers_from_stale_current_version(self):
        self.page.current_version = 0
        self.page.status = CalendarPage.Status.DISABLED
        self.page.save(
            update_fields=["current_version", "status", "updated_at"]
        )

        version = publish_page(page=self.page, actor=self.user)

        self.page.refresh_from_db()
        self.assertEqual(version.version, 2)
        self.assertEqual(self.page.current_version, 2)
        self.assertEqual(self.page.status, CalendarPage.Status.PUBLISHED)

    def test_status_toggle_recovers_legacy_stale_version_state(self):
        self.page.current_version = 0
        self.page.status = CalendarPage.Status.DISABLED
        self.page.save(
            update_fields=["current_version", "status", "updated_at"]
        )
        self._authenticate_dashboard_client()

        response = self.client.post(
            reverse(
                "shvya_calendar:status",
                kwargs={"page_id": self.page.id},
            ),
            {"action": "publish"},
        )

        self.assertEqual(response.status_code, 302)
        self.page.refresh_from_db()
        self.assertEqual(self.page.status, CalendarPage.Status.PUBLISHED)
        self.assertEqual(self.page.current_version, 2)

    @patch("apps.shvya_calendar.models.CalendarPage.full_clean")
    def test_status_toggle_does_not_depend_on_model_full_clean(
        self,
        mocked_full_clean,
    ):
        mocked_full_clean.side_effect = RuntimeError("legacy validator failure")
        self.page.status = CalendarPage.Status.DISABLED
        self.page.save(update_fields=["status", "updated_at"])
        self._authenticate_dashboard_client()

        response = self.client.post(
            reverse(
                "shvya_calendar:status",
                kwargs={"page_id": self.page.id},
            ),
            {"action": "publish"},
        )

        self.assertEqual(response.status_code, 302)
        self.page.refresh_from_db()
        self.assertEqual(self.page.status, CalendarPage.Status.PUBLISHED)
        mocked_full_clean.assert_not_called()

    def test_calendar_attachment_is_visible_on_lead_card_and_downloadable(self):
        lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Attachment Lead",
            phone="+919111111111",
            lead_source="shvya_calendar",
        )
        submission = CalendarSubmission.objects.create(
            organization=self.organization,
            page=self.page,
            page_version=self.version,
            lead=lead,
            status=CalendarSubmission.Status.LEAD_CREATED,
            submitted_data={},
            normalized_data={"name": lead.name, "mobile": lead.phone},
        )
        attachment = CalendarSubmissionAttachment.objects.create(
            submission=submission,
            field_key="requirements",
            file=SimpleUploadedFile(
                "requirements.pdf",
                b"%PDF-1.4\ncalendar attachment\n",
                content_type="application/pdf",
            ),
            original_name="requirements.pdf",
            content_type="application/pdf",
            size=31,
        )
        self._authenticate_dashboard_client()

        from apps.crm.views.dashboard import _lead_card_context
        context = _lead_card_context(lead, self.user)
        context["all_stages"] = [self.stage]
        html = render_to_string("crm/partials/lead_card.html", context)

        self.assertEqual(context["lead"].calendar_attachment_count, 1)
        self.assertIn("requirements.pdf", html)
        self.assertIn(
            reverse(
                "shvya_calendar:attachment_download",
                kwargs={"attachment_id": attachment.id},
            ),
            html,
        )

        response = self.client.get(
            reverse(
                "shvya_calendar:attachment_download",
                kwargs={"attachment_id": attachment.id},
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("attachment", response["Content-Disposition"])

    def test_share_always_shows_website_embed_code(self):
        self.page.status = CalendarPage.Status.DISABLED
        self.page.save(update_fields=["status", "updated_at"])
        self._authenticate_dashboard_client()

        response = self.client.get(
            reverse(
                "shvya_calendar:editor",
                kwargs={"page_id": self.page.id},
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Place the complete flow inside your website",
        )
        self.assertContains(response, "Copy embed")
        self.assertContains(response, "<iframe", html=False)

    def test_logo_upload_is_saved_and_published_in_snapshot(self):
        self._authenticate_dashboard_client()
        logo = SimpleUploadedFile(
            "brand.png",
            b"SHVYA-EDITOR-EXPORTED-IMAGE-BYTES",
            content_type="image/png",
        )

        response = self.client.post(
            reverse(
                "shvya_calendar:save",
                kwargs={"page_id": self.page.id},
            ),
            {
                "section": "lead",
                "page_type": self.page.page_type,
                "name": self.page.name,
                "slug": self.page.slug,
                "accent_color": self.page.accent_color,
                "language": "en",
                "pipeline": str(self.pipeline.id),
                "stage": str(self.stage.id),
                "host": str(self.user.id),
                "submit_button_text": self.page.submit_button_text,
                "duplicate_behavior": self.page.duplicate_behavior,
                "duplicate_match_email": "on",
                "attribute_update_policy": self.page.attribute_update_policy,
                "notify_host_on_submission": "on",
                "consent_enabled": "on",
                "consent_text": self.page.consent_text,
                "form_schema": json.dumps(self.page.form_schema),
                "logo_file": logo,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.page.refresh_from_db()
        self.assertTrue(self.page.logo_file.name)
        self.assertIn("/media/calendar/logos/", self.page.logo_display_url)
        version = self.page.published_versions.order_by("-version").first()
        self.assertEqual(
            version.snapshot["logo_url"],
            self.page.logo_display_url,
        )

    def test_google_meet_page_can_publish_before_google_connection(self):
        self.page.meeting_location = CalendarPage.MeetingLocation.GOOGLE_MEET
        self.page.status = CalendarPage.Status.DISABLED
        self.page.save(update_fields=["meeting_location", "status", "updated_at"])

        version = publish_page(page=self.page, actor=self.user)

        self.page.refresh_from_db()
        self.assertEqual(self.page.status, CalendarPage.Status.PUBLISHED)
        self.assertEqual(version.version, self.page.current_version)
        with self.assertRaises(GoogleCalendarError):
            available_slots(
                page=self.page,
                local_date=timezone.localdate() + timedelta(days=1),
            )

    def test_status_toggle_publishes_google_meet_page_without_http_500(self):
        self.page.meeting_location = CalendarPage.MeetingLocation.GOOGLE_MEET
        self.page.status = CalendarPage.Status.DISABLED
        self.page.save(update_fields=["meeting_location", "status", "updated_at"])
        self._authenticate_dashboard_client()

        response = self.client.post(
            reverse("shvya_calendar:status", kwargs={"page_id": self.page.id}),
            {"action": "publish"},
        )

        self.assertEqual(response.status_code, 302)
        self.page.refresh_from_db()
        self.assertEqual(self.page.status, CalendarPage.Status.PUBLISHED)

        public_response = self.client.get(
            reverse(
                "shvya_calendar_public:page",
                kwargs={"public_id": self.page.public_id, "slug": self.page.slug},
            )
        )
        self.assertEqual(public_response.status_code, 200)
        self.assertContains(public_response, "Tell us how to reach you")

    def test_scheduling_save_succeeds_for_published_page_without_google_connection(self):
        self.page.meeting_location = CalendarPage.MeetingLocation.GOOGLE_MEET
        self.page.save(update_fields=["meeting_location", "updated_at"])
        publish_page(page=self.page, actor=self.user)
        self.page.refresh_from_db()
        version_before = self.page.current_version
        self._authenticate_dashboard_client()

        response = self.client.post(
            reverse("shvya_calendar:save", kwargs={"page_id": self.page.id}),
            {
                "section": "scheduling",
                "timezone": "Asia/Kolkata",
                "session_title": "Updated Demo",
                "session_description": "Calendar regression",
                "discussion_points": "Review goals\nNext steps",
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
                "meeting_location": "google_meet",
                "invite_lead_to_event": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.page.refresh_from_db()
        self.assertEqual(self.page.session_title, "Updated Demo")
        self.assertEqual(self.page.status, CalendarPage.Status.PUBLISHED)
        self.assertGreater(self.page.current_version, version_before)

    def test_scheduling_editor_has_no_nested_action_forms(self):
        CalendarBlock.objects.create(
            page=self.page,
            starts_at=timezone.now() + timedelta(days=3),
            ends_at=timezone.now() + timedelta(days=3, hours=1),
            reason="Maintenance",
        )
        self._authenticate_dashboard_client()

        response = self.client.get(
            reverse("shvya_calendar:editor", kwargs={"page_id": self.page.id})
            + "?tab=scheduling"
        )

        self.assertEqual(response.status_code, 200)
        delete_url = reverse(
            "shvya_calendar:block_delete",
            kwargs={
                "page_id": self.page.id,
                "block_id": self.page.blocks.first().id,
            },
        )
        self.assertContains(response, f'formaction="{delete_url}"')
        self.assertNotContains(
            response,
            f'<form method="post" action="{delete_url}"',
        )

    def test_email_match_with_different_mobile_is_not_silently_reused(self):
        Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Existing Email",
            phone="+917000000099",
            email="same@example.com",
        )
        request = self._request(
            {
                "name": "New Mobile",
                "mobile": "+917000000100",
                "consent": "on",
                "f_email": "same@example.com",
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

    def test_public_link_is_friendly_when_page_is_not_published(self):
        self.page.status = CalendarPage.Status.DISABLED
        self.page.save(update_fields=["status", "updated_at"])

        response = self.client.get(
            reverse(
                "shvya_calendar_public:page",
                kwargs={"public_id": self.page.public_id, "slug": self.page.slug},
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "currently unavailable")

    def test_old_public_slug_still_resolves_after_slug_change(self):
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
        self.assertContains(response, "Tell us how to reach you")

    def test_invalid_timezone_is_rejected_by_page_validation(self):
        self.page.timezone = "Not/A-Timezone"
        with self.assertRaises(ValidationError):
            self.page.full_clean()

    def test_invalid_working_hours_are_rejected(self):
        availability = dict(self.page.availability)
        availability["mon"] = {
            "enabled": True,
            "start": "18:00",
            "end": "09:00",
        }
        self.page.availability = availability
        with self.assertRaises(ValidationError):
            self.page.full_clean()

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

    @patch("apps.shvya_calendar.services.create_booking_event")
    @patch("apps.shvya_calendar.services.available_slots")
    def test_booking_retry_reuses_existing_active_booking(
        self,
        mocked_available_slots,
        mocked_create_event,
    ):
        Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Retry Lead",
            phone="+918111111111",
            lead_source="shvya_calendar",
        )
        request = self._request(
            {
                "name": "Retry Lead",
                "mobile": "+918111111111",
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
        self.page.meeting_location = CalendarPage.MeetingLocation.PHONE
        self.page.save(update_fields=["meeting_location", "updated_at"])

        slot_start = timezone.now() + timedelta(days=2)
        slot_start = slot_start.replace(second=0, microsecond=0)
        mocked_available_slots.return_value = [
            {
                "start": slot_start,
                "end": slot_start + timedelta(minutes=30),
                "label": "10:00 AM",
            }
        ]

        first = book_slot(
            page=self.page,
            submission=submission,
            slot_start_iso=slot_start.isoformat(),
        )
        second = book_slot(
            page=self.page,
            submission=submission,
            slot_start_iso=slot_start.isoformat(),
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(
            CalendarBooking.objects.filter(
                submission=submission,
                status__in=[
                    CalendarBooking.Status.SCHEDULED,
                    CalendarBooking.Status.RESCHEDULED,
                ],
            ).count(),
            1,
        )
        mocked_create_event.assert_called_once()
