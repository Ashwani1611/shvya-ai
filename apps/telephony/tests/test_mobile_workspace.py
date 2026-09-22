from datetime import timedelta

from django.contrib.sessions.backends.db import SessionStore
from django.test import Client, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.accounts.session_utils import get_session_cookie_name, set_authenticated_user
from apps.crm.models import LeadReminder
from apps.telephony.models import CallDevice, CallRecord, CallIntelligenceSettings
from apps.telephony.services import ingest_call_event, register_device
from . import test_call_intelligence as fixtures


class MobileWorkspaceTests(TestCase):
    setUp = fixtures.CallIntelligenceTests.setUp
    payload = fixtures.CallIntelligenceTests.payload

    def api(self, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.user)
        return client

    def web(self, user=None):
        session = SessionStore()
        set_authenticated_user(session, user or self.user)
        session.save()
        client = Client()
        client.cookies[get_session_cookie_name("dashboard")] = session.session_key
        return client

    def call(self, **kwargs):
        return ingest_call_event(user=self.user, payload=self.payload(**kwargs))["call"]

    def test_dashboard_filters_apply_to_metrics_and_history(self):
        self.call(status="answered")
        self.call(status="missed", talk_duration_seconds=0, ring_duration_seconds=20,
                  ended_at=(timezone.now() - timedelta(days=2)).isoformat())
        response = self.web().get("/dashboard/call-intelligence/", {"section": "analytics", "status": "missed"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["stats"]["total"], 1)
        self.assertEqual(response.context["stats"]["total_ring"], 20)
        self.assertEqual(len(response.context["recent_calls"]), 1)
        self.assertContains(response, "Rang: 20s")
        self.assertContains(response, "shvya_premium_sidebar_mobile.css")
        self.assertNotContains(response, "Download for Android")

    def test_date_filter_updates_metrics_and_activity_together(self):
        self.call(ring_duration_seconds=8)
        self.call(ring_duration_seconds=30, ended_at=(timezone.now() - timedelta(days=3)).isoformat())
        response = self.web().get("/dashboard/call-intelligence/", {
            "date_from": timezone.localdate().isoformat(), "section": "analytics",
        })
        self.assertEqual(response.context["stats"]["total"], 1)
        self.assertEqual(response.context["stats"]["total_ring"], 8)
        self.assertEqual(len(response.context["recent_calls"]), 1)

    def test_unknown_ringing_not_reported_as_zero_measurement(self):
        self.call(ring_duration_seconds=0)
        response = self.web().get("/dashboard/call-intelligence/?section=analytics")
        self.assertEqual(response.context["stats"]["measured_ring"], 0)
        self.assertContains(response, "Rang: Not available")

    def test_remove_device_stops_automatic_registration_and_ingestion(self):
        call = self.call()
        device = CallDevice.objects.get(device_id="android-001")
        response = self.web().post(f"/dashboard/call-intelligence/devices/{device.id}/remove/")
        self.assertEqual(response.status_code, 302)
        api = self.api()
        response = api.post("/api/v1/call-intelligence/devices/register/", {"device_id": device.device_id}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "device_removed")
        response = api.post("/api/v1/call-intelligence/events/", self.payload(), format="json")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(CallRecord.objects.filter(pk=call.pk).exists())
        self.assertEqual(CallRecord.objects.count(), 1)

    def test_device_removal_is_post_only_and_tenant_scoped(self):
        device = CallDevice.objects.get(device_id="android-001")
        url = f"/dashboard/call-intelligence/devices/{device.id}/remove/"
        self.assertEqual(self.web().get(url).status_code, 405)
        self.assertEqual(self.web(self.other_user).post(url).status_code, 404)
        device.refresh_from_db()
        self.assertTrue(device.is_active)

    def test_agent_cannot_remove_another_employees_device(self):
        self.user.role = User.Role.AGENT
        self.user.save(update_fields=["role"])
        peer = User.objects.create_user(email="peer@example.com", organization=self.org, password="secret123")
        device = register_device(user=peer, payload={"device_id": "peer-device"})
        self.assertEqual(self.web().post(f"/dashboard/call-intelligence/devices/{device.id}/remove/").status_code, 404)
        self.assertNotContains(self.web().get("/dashboard/call-intelligence/"), "peer-device")

    def test_mobile_call_summary_is_filtered_and_scoped(self):
        self.call()
        self.call(status="missed", talk_duration_seconds=0)
        response = self.api().get("/api/v1/call-intelligence/calls/?mine=1&status=missed")
        self.assertEqual(response.data["stats"]["total"], 1)
        self.assertEqual(response.data["stats"]["not_picked"], 1)
        self.assertEqual(len(response.data["calls"]), 1)
        self.assertEqual(self.api(self.other_user).get("/api/v1/call-intelligence/calls/").data["stats"]["total"], 0)

    def test_mobile_pagination_keeps_full_summary(self):
        call = self.call()
        now = timezone.now()
        CallRecord.objects.bulk_create([CallRecord(
            organization=self.org, user=self.user, phone_number=f"+91999999{i:04}",
            source="cloud", source_call_id=f"page-{i}", direction="incoming", status="missed", ended_at=now,
        ) for i in range(51)])
        response = self.api().get("/api/v1/call-intelligence/calls/?mine=1")
        self.assertEqual(len(response.data["calls"]), 50)
        self.assertEqual(response.data["stats"]["total"], 52)
        self.assertTrue(response.data["has_next"])
        second = self.api().get("/api/v1/call-intelligence/calls/?mine=1&page=2")
        self.assertEqual(len(second.data["calls"]), 2)
        self.assertFalse(second.data["has_next"])
        self.assertTrue(CallRecord.objects.filter(pk=call.pk).exists())

    def reminder(self):
        call = self.call()
        call.follow_up_required = True
        call.save(update_fields=["follow_up_required"])
        row = LeadReminder.objects.create(lead=call.lead, assigned_to=self.user, title="Call back",
                                         due_at=timezone.now() - timedelta(days=2))
        return call, row

    def test_reminder_snooze_uses_now_for_overdue_and_updates_call(self):
        call, row = self.reminder()
        response = self.api().post(f"/api/v1/call-intelligence/reminders/{row.id}/action/", {"action": "snooze"}, format="json")
        self.assertEqual(response.status_code, 200)
        row.refresh_from_db()
        call.refresh_from_db()
        self.assertGreater(row.due_at, timezone.now() + timedelta(minutes=29))
        self.assertEqual(call.follow_up_at, row.due_at)

    def test_reminder_complete_updates_canonical_reminder_and_call(self):
        call, row = self.reminder()
        response = self.api().post(f"/api/v1/call-intelligence/reminders/{row.id}/action/", {"action": "complete"}, format="json")
        self.assertEqual(response.status_code, 200)
        row.refresh_from_db()
        call.refresh_from_db()
        self.assertEqual(row.status, "completed")
        self.assertFalse(call.follow_up_required)
        self.assertEqual(self.api().get("/api/v1/call-intelligence/reminders/").data["stats"]["total"], 0)

    def test_reminder_delete_and_cross_tenant_denial(self):
        _, row = self.reminder()
        url = f"/api/v1/call-intelligence/reminders/{row.id}/action/"
        self.assertEqual(self.api(self.other_user).get("/api/v1/call-intelligence/reminders/").data["stats"]["total"], 0)
        self.assertEqual(self.api(self.other_user).post(url, {"action": "delete"}, format="json").status_code, 404)
        self.assertEqual(self.api().post(url, {"action": "delete"}, format="json").status_code, 200)
        self.assertFalse(LeadReminder.objects.filter(pk=row.id).exists())

    def test_settings_patch_requires_admin_and_preserves_other_rules(self):
        self.user.role = User.Role.AGENT
        self.user.save(update_fields=["role"])
        url = "/api/v1/call-intelligence/settings/"
        self.assertEqual(self.api().patch(url, {"auto_create_missed": False}, format="json").status_code, 403)
        self.user.role = User.Role.ADMIN
        self.user.save(update_fields=["role"])
        settings = CallIntelligenceSettings.objects.get(organization=self.org)
        before = settings.auto_create_answered_incoming
        self.assertEqual(self.api().patch(url, {"auto_create_missed": False}, format="json").status_code, 200)
        settings.refresh_from_db()
        self.assertFalse(settings.auto_create_missed)
        self.assertEqual(settings.auto_create_answered_incoming, before)
        self.assertEqual(self.api().patch(url, {"auto_create_missed": "false"}, format="json").status_code, 400)

    def test_add_lead_preserves_existing_crm_route(self):
        call = self.call()
        url = "/api/v1/call-intelligence/leads/"
        response = self.api().post(url, {"name": "Replacement", "phone": call.phone_number}, format="json")
        self.assertEqual(response.status_code, 409)
        call.lead.refresh_from_db()
        self.assertNotEqual(call.lead.name, "Replacement")
        response = self.api().post(url, {"name": "New prospect", "phone": "9123456789"}, format="json")
        self.assertEqual(response.status_code, 201)
