from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.core.context_processors import NAV_ITEMS, _resolve_active
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from .models import (
    CalendarBooking,
    CalendarPage,
    CalendarPageVersion,
    CalendarSubmission,
)
from .services import move_booking_pipeline, reschedule_booking


class CalendarWorkspaceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            package="enterprise",
            name="Calendar workspace", timezone="Asia/Kolkata"
        )
        self.user = User.objects.create_user(
            email="workspace@example.com",
            password="test",
            name="Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.org, name="Sales", owner=self.user
        )
        self.stage = Stage.objects.create(
            pipeline=self.pipeline, name="New Lead", display_order=0
        )
        self.lead = Lead.objects.create(
            organization=self.org,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Jane",
            phone="+919876543210",
            email="jane@example.com",
        )
        self.page = CalendarPage.objects.create(
            organization=self.org,
            name="Demo",
            slug="demo",
            host=self.user,
            pipeline=self.pipeline,
            stage=self.stage,
            timezone="Asia/Kolkata",
            meeting_location="custom",
            custom_meeting_link="https://meet.example.com/demo",
        )
        self.version = CalendarPageVersion.objects.create(
            page=self.page,
            version=1,
            snapshot={
                "meeting_location": "custom",
                "custom_meeting_link": "https://meet.example.com/demo",
                "discussion_points": ["Product walkthrough"],
                "form_schema": [{"key": "company", "label": "Company"}],
            },
        )
        self.submission = CalendarSubmission.objects.create(
            organization=self.org,
            page=self.page,
            page_version=self.version,
            lead=self.lead,
            status="lead_created",
            submitted_data={"company": "Example Ltd"},
        )
        self.booking = CalendarBooking.objects.create(
            organization=self.org,
            page=self.page,
            submission=self.submission,
            lead=self.lead,
            host=self.user,
            timezone="Asia/Kolkata",
            start_at=datetime(2026, 9, 22, 18, 45, tzinfo=UTC),
            end_at=datetime(2026, 9, 22, 19, 15, tzinfo=UTC),
        )
        self.authenticate(self.user)

    def authenticate(self, user):
        session = SessionStore()
        set_authenticated_user(session, user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    def url(self, name):
        return reverse(f"shvya_calendar:{name}", kwargs={"booking_id": self.booking.pk})

    def test_workspace_and_booking_routes_render(self):
        self.assertContains(
            self.client.get(reverse("shvya_calendar:calendar")), "All pipelines"
        )
        self.assertContains(
            self.client.get(reverse("shvya_calendar:bookings")), "Turn interest"
        )

    def test_range_uses_organization_midnight(self):
        endpoint = reverse("shvya_calendar:events")
        before = self.client.get(endpoint, {"start": "2026-09-22", "end": "2026-09-23"})
        after = self.client.get(endpoint, {"start": "2026-09-23", "end": "2026-09-24"})
        self.assertEqual(before.json()["events"], [])
        self.assertEqual(after.json()["events"][0]["id"], str(self.booking.pk))
        self.assertNotIn("cancel_token", str(after.json()))

    def test_overlap_at_range_start_is_included(self):
        self.booking.start_at = datetime(2026, 9, 22, 18, 0, tzinfo=UTC)
        self.booking.save()
        data = self.client.get(
            reverse("shvya_calendar:events"),
            {"start": "2026-09-23", "end": "2026-09-24"},
        ).json()
        self.assertEqual(len(data["events"]), 1)

    def test_invalid_ranges_are_rejected(self):
        for params in [
            {},
            {"start": "bad", "end": "bad"},
            {"start": "2026-01-01", "end": "2027-01-01"},
            {"start": "2026-09-23", "end": "2026-09-24", "offset": "-1"},
            {"start": "2026-09-23", "end": "2026-09-24", "pipeline": "bad"},
        ]:
            self.assertEqual(
                self.client.get(reverse("shvya_calendar:events"), params).status_code,
                400,
            )

    def test_detail_has_responses_meeting_and_actions(self):
        response = self.client.get(self.url("booking_detail"))
        for text in [
            "Example Ltd",
            "Company",
            "Product walkthrough",
            "Join meeting",
            "https://meet.example.com/demo",
            "Move to another pipeline",
            "Reschedule booking",
        ]:
            self.assertContains(response, text)

    def test_detail_escapes_untrusted_text_and_unsafe_url(self):
        self.submission.submitted_data = {"company": "<script>alert(1)</script>"}
        self.submission.save()
        self.booking.meeting_link = "javascript:alert(1)"
        self.booking.save()
        self.version.snapshot["custom_meeting_link"] = "javascript:alert(2)"
        self.version.save()
        response = self.client.get(self.url("booking_detail"))
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "javascript:")

    def test_other_organization_cannot_read_or_change(self):
        org = Organization.objects.create(package="enterprise", name="Other")
        other = User.objects.create_user(
            email="other@example.com",
            password="test",
            name="Other",
            organization=org,
            role=User.Role.ADMIN,
        )
        self.authenticate(other)
        for name in ["booking_detail", "booking_slots"]:
            self.assertEqual(self.client.get(self.url(name)).status_code, 404)
        self.assertEqual(
            self.client.post(
                self.url("booking_update"), {"action": "move"}
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse("shvya_calendar:events"),
                {"start": "2026-09-23", "end": "2026-09-24"},
            ).json()["events"],
            [],
        )
        self.assertEqual(
            self.client.get(
                reverse("shvya_calendar:events"),
                {
                    "start": "2026-09-23",
                    "end": "2026-09-24",
                    "pipeline": str(self.pipeline.pk),
                },
            ).status_code,
            404,
        )

    def test_agent_is_not_granted_admin_calendar_access(self):
        self.user.role = User.Role.AGENT
        self.user.save()
        for path in [
            reverse("shvya_calendar:calendar"),
            reverse("shvya_calendar:events"),
            self.url("booking_detail"),
        ]:
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_move_uses_crm_transition_and_filter_follows_lead(self):
        target = Pipeline.objects.create(
            organization=self.org, owner=self.user, name="Support"
        )
        stage = Stage.objects.create(pipeline=target, name="New Lead", display_order=0)
        response = self.client.post(
            self.url("booking_update"),
            {"action": "move", "pipeline": target.pk, "stage": stage.pk},
        )
        self.assertEqual(response.status_code, 200)
        self.lead.refresh_from_db()
        self.booking.refresh_from_db()
        self.assertEqual(self.lead.pipeline_id, target.pk)
        self.assertEqual(self.booking.page_id, self.page.pk)
        endpoint = reverse("shvya_calendar:events")
        params = {
            "start": "2026-09-23",
            "end": "2026-09-24",
            "pipeline": str(target.pk),
        }
        self.assertEqual(len(self.client.get(endpoint, params).json()["events"]), 1)
        params["pipeline"] = str(self.pipeline.pk)
        self.assertEqual(self.client.get(endpoint, params).json()["events"], [])

    def test_move_rejects_stage_from_wrong_pipeline_and_foreign_target(self):
        other = Organization.objects.create(package="enterprise", name="Other")
        target = Pipeline.objects.create(organization=other, name="Other")
        for pipeline_id, stage_id in [
            (target.pk, self.stage.pk),
            (self.pipeline.pk, "invalid"),
        ]:
            response = self.client.post(
                self.url("booking_update"),
                {"action": "move", "pipeline": pipeline_id, "stage": stage_id},
            )
            self.assertEqual(response.status_code, 400)
        with self.assertRaises(ValidationError):
            move_booking_pipeline(
                booking=self.booking,
                actor=self.user,
                pipeline_id=target.pk,
                stage_id=self.stage.pk,
            )
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.pipeline_id, self.pipeline.pk)

    @patch("apps.shvya_calendar.workspace.reschedule_booking")
    def test_reschedule_delegates_to_existing_service(self, reschedule):
        response = self.client.post(
            self.url("booking_update"),
            {"action": "reschedule", "slot_start": "2026-09-25T10:00:00+00:00"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(reschedule.call_args.kwargs["booking"].pk, self.booking.pk)
        self.assertEqual(
            reschedule.call_args.kwargs["slot_start_iso"], "2026-09-25T10:00:00+00:00"
        )
        reschedule.side_effect = ValidationError("That slot is no longer available.")
        self.assertEqual(
            self.client.post(
                self.url("booking_update"),
                {"action": "reschedule", "slot_start": "bad"},
            ).status_code,
            400,
        )

    @patch("apps.shvya_calendar.services.available_slots")
    def test_terminal_booking_cannot_reschedule_after_stale_read(self, slots):
        requested = self.booking.start_at + timedelta(days=1)
        slots.return_value = [{"start": requested}]
        CalendarBooking.objects.filter(pk=self.booking.pk).update(status="completed")
        with self.assertRaises(ValidationError):
            reschedule_booking(
                booking=self.booking, slot_start_iso=requested.isoformat()
            )

    @patch("apps.shvya_calendar.workspace.available_slots")
    def test_slots_use_availability_service(self, slots):
        from django.utils import timezone

        day = timezone.now().astimezone(ZoneInfo(self.org.timezone)).date().isoformat()
        slots.return_value = [{"start": self.booking.start_at, "label": "12:15 AM"}]
        response = self.client.get(self.url("booking_slots"), {"date": day})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["slots"][0]["value"], self.booking.start_at.isoformat()
        )
        self.assertEqual(
            self.client.get(self.url("booking_slots"), {"date": "bad"}).status_code, 400
        )

    def test_csrf_and_methods_are_enforced(self):
        secure = Client(enforce_csrf_checks=True)
        secure.cookies = self.client.cookies
        self.assertEqual(
            secure.post(self.url("booking_update"), {"action": "move"}).status_code, 403
        )
        self.assertEqual(self.client.get(self.url("booking_update")).status_code, 405)

    def test_calendar_and_booking_have_separate_active_navigation(self):
        item = next(i for i in NAV_ITEMS if i["label"] == "SHVYA Calendar")
        booking, calendar = item["children"]
        self.assertTrue(_resolve_active(booking, reverse("shvya_calendar:bookings")))
        self.assertFalse(_resolve_active(booking, reverse("shvya_calendar:calendar")))
        self.assertTrue(_resolve_active(calendar, reverse("shvya_calendar:calendar")))
