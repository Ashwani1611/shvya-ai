import uuid
from datetime import timedelta

from django.contrib.sessions.backends.db import SessionStore
from django.test import Client, TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from unittest.mock import patch

from apps.accounts.models import User
from apps.accounts.session_utils import get_session_cookie_name, set_authenticated_user
from apps.crm.models import Lead, LeadReminder, Pipeline
from apps.organizations.models import Organization
from apps.telephony.models import (
    CallDevice,
    CallDisposition,
    CallEvent,
    CallIntelligenceSettings,
    CallRecord,
)
from apps.telephony.services import ingest_call_event, register_device


class CallIntelligenceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(package="enterprise", name="Call Intelligence Org")
        self.other_org = Organization.objects.create(package="enterprise", name="Other Org")
        self.user = User.objects.create_user(
            email="agent@example.com",
            name="Agent",
            organization=self.org,
            password="secret123",
        )
        self.other_user = User.objects.create_user(
            email="other@example.com",
            name="Other",
            organization=self.other_org,
            password="secret123",
        )
        self.pipeline = Pipeline.objects.get(
            organization=self.org,
            name="Leads",
        )
        self.pipeline.owner = self.user
        self.pipeline.country_code = "+91"
        self.pipeline.save(update_fields=["owner", "country_code", "updated_at"])
        self.stage = self.pipeline.stages.filter(is_active=True).order_by(
            "display_order", "name"
        ).first()
        self.assertIsNotNone(self.stage)
        CallIntelligenceSettings.objects.create(
            organization=self.org,
            default_pipeline=self.pipeline,
            default_stage=self.stage,
        )
        register_device(
            user=self.user,
            payload={"device_id": "android-001", "name": "Sales phone"},
        )

    def payload(self, **updates):
        now = timezone.now()
        payload = {
            "event_uuid": str(uuid.uuid4()),
            "device_id": "android-001",
            "source_call_id": f"log-{uuid.uuid4()}",
            "phone_number": "9876543210",
            "raw_phone_number": "9876543210",
            "contact_name": "Prospect",
            "direction": "incoming",
            "status": "answered",
            "event_type": "completed",
            "occurred_at": now.isoformat(),
            "started_at": (now - timedelta(seconds=90)).isoformat(),
            "answered_at": (now - timedelta(seconds=80)).isoformat(),
            "ended_at": now.isoformat(),
            "ring_duration_seconds": 10,
            "talk_duration_seconds": 80,
            "total_duration_seconds": 90,
        }
        payload.update(updates)
        return payload

    def test_completed_unknown_number_creates_phone_call_lead_and_crm_call(self):
        result = ingest_call_event(user=self.user, payload=self.payload())
        self.assertTrue(result["lead_created"])
        call = result["call"]
        self.assertEqual(call.organization, self.org)
        self.assertEqual(call.phone_number, "+919876543210")
        self.assertIsNotNone(call.lead_id)
        self.assertEqual(call.lead.lead_source, "phone_call")
        self.assertEqual(call.lead.pipeline, self.pipeline)
        self.assertEqual(call.lead.stage, self.stage)
        self.assertIsNotNone(call.crm_call_id)
        self.assertEqual(call.crm_call.status, "completed")
        self.assertEqual(call.crm_call.duration_seconds, 80)

    def test_event_uuid_is_idempotent(self):
        payload = self.payload()
        first = ingest_call_event(user=self.user, payload=payload)
        second = ingest_call_event(user=self.user, payload=payload)
        self.assertTrue(first["event_created"])
        self.assertFalse(second["event_created"])
        self.assertEqual(first["call"].id, second["call"].id)
        self.assertEqual(CallEvent.objects.count(), 1)
        self.assertEqual(CallRecord.objects.count(), 1)
        self.assertEqual(Lead.objects.count(), 1)

    def test_existing_lead_is_reused_not_duplicated(self):
        lead = Lead.objects.create(
            organization=self.org,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Existing",
            phone="+919876543210",
        )
        result = ingest_call_event(user=self.user, payload=self.payload())
        self.assertFalse(result["lead_created"])
        self.assertEqual(result["call"].lead_id, lead.id)
        self.assertEqual(Lead.objects.filter(organization=self.org).count(), 1)

    def test_device_cannot_move_to_other_organization(self):
        with self.assertRaisesMessage(Exception, "already registered"):
            ingest_call_event(
                user=self.other_user,
                payload=self.payload(device_id="android-001"),
            )

    def test_same_source_call_id_reuses_call_with_new_event(self):
        first_payload = self.payload()
        second_payload = self.payload(source_call_id=first_payload["source_call_id"])
        first = ingest_call_event(user=self.user, payload=first_payload)
        second = ingest_call_event(user=self.user, payload=second_payload)
        self.assertNotEqual(first["event"].event_uuid, second["event"].event_uuid)
        self.assertEqual(first["call"].id, second["call"].id)
        self.assertEqual(CallRecord.objects.count(), 1)
        self.assertEqual(CallEvent.objects.count(), 2)
        self.assertEqual(first["call"].crm_call_id, second["call"].crm_call_id)

    def test_cloud_call_uses_universal_backend_without_android_device(self):
        result = ingest_call_event(
            user=self.user,
            payload=self.payload(
                source="cloud",
                device_id="",
                provider="twilio",
                provider_call_id="provider-123",
                source_call_id="provider-call-123",
                transcript="Agent: Hello\nLead: Please schedule a demo.",
                transcript_status="completed",
            ),
        )
        call = result["call"]
        self.assertEqual(call.source, "cloud")
        self.assertIsNone(call.device_id)
        self.assertEqual(call.provider, "twilio")
        self.assertEqual(call.provider_call_id, "provider-123")
        self.assertIn("schedule a demo", call.transcript)
        self.assertIsNotNone(call.lead_id)

    def test_missed_call_auto_creates_when_enabled(self):
        result = ingest_call_event(
            user=self.user,
            payload=self.payload(
                status="missed",
                event_type="missed",
                answered_at=None,
                talk_duration_seconds=0,
            ),
        )
        self.assertTrue(result["lead_created"])
        self.assertEqual(result["call"].crm_call.status, "no_response")


class CallIntelligenceApiTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(package="enterprise", name="API Call Org")
        self.user = User.objects.create_user(
            email="api-agent@example.com",
            name="API Agent",
            organization=self.org,
            role="admin",
            password="secret123",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_register_device_ignores_client_organization_id(self):
        response = self.client.post(
            "/api/v1/call-intelligence/devices/register/",
            {
                "device_id": "phone-api-1",
                "name": "Pixel",
                "organization_id": str(uuid.uuid4()),
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        device = CallDevice.objects.get(device_id="phone-api-1")
        self.assertEqual(device.organization_id, self.org.id)
        self.assertEqual(device.user_id, self.user.id)


    def test_dispositions_are_tenant_scoped_and_customizable(self):
        response = self.client.get("/api/v1/call-intelligence/dispositions/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["dispositions"])

        response = self.client.post(
            "/api/v1/call-intelligence/dispositions/",
            {"name": "Demo booked", "category": "connected", "position": 2},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        row = CallDisposition.objects.get(organization=self.org, code="demo-booked")
        self.assertEqual(row.name, "Demo booked")

        other = Organization.objects.create(package="enterprise", name="Other disposition org")
        self.assertFalse(CallDisposition.objects.filter(organization=other, code=row.code).exists())

    @patch("apps.telephony.tasks.analyze_call_intelligence.delay")
    def test_provider_media_update_stores_transcript_and_queues_analysis(self, analyze):
        pipeline = self.org.pipelines.first()
        stage = pipeline.stages.filter(is_active=True).order_by("display_order").first()
        lead = Lead.objects.create(
            organization=self.org,
            pipeline=pipeline,
            stage=stage,
            name="Media Lead",
            phone="+919876543210",
        )
        call = CallRecord.objects.create(
            organization=self.org,
            user=self.user,
            lead=lead,
            source=CallRecord.Source.CLOUD,
            source_call_id="media-1",
            phone_number=lead.phone,
            direction=CallRecord.Direction.INCOMING,
            status=CallRecord.Status.ANSWERED,
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                f"/api/v1/call-intelligence/calls/{call.id}/media/",
                {
                    "recording_url": "https://example.com/call.mp3",
                    "transcript": "Agent: Hello\nLead: I want a demo.",
                    "transcript_speakers": [{"speaker": "agent"}, {"speaker": "lead"}],
                },
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        call.refresh_from_db()
        self.assertEqual(call.recording_status, "ready")
        self.assertEqual(call.transcript_status, "completed")
        self.assertIn("want a demo", call.transcript)
        analyze.assert_called_once_with(str(call.id), call.analysis_input_hash)

    def test_call_follow_up_replaces_existing_lead_reminder(self):
        pipeline = self.org.pipelines.first()
        stage = pipeline.stages.filter(is_active=True).order_by("display_order").first()
        lead = Lead.objects.create(
            organization=self.org,
            pipeline=pipeline,
            stage=stage,
            name="Reminder Lead",
            phone="+919123456789",
        )
        old = LeadReminder.objects.create(
            lead=lead,
            assigned_to=self.user,
            title="Old",
            due_at=timezone.now() + timedelta(hours=1),
        )
        call = CallRecord.objects.create(
            organization=self.org,
            user=self.user,
            lead=lead,
            source=CallRecord.Source.ANDROID_SIM,
            source_call_id="reminder-call",
            phone_number=lead.phone,
            direction=CallRecord.Direction.OUTGOING,
            status=CallRecord.Status.ANSWERED,
        )
        due = timezone.now() + timedelta(hours=3)
        response = self.client.post(
            f"/api/v1/call-intelligence/calls/{call.id}/follow-up/",
            {"due_at": due.isoformat(), "title": "New follow-up"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertFalse(LeadReminder.objects.filter(pk=old.pk).exists())
        self.assertEqual(LeadReminder.objects.filter(lead=lead).count(), 1)
        self.assertEqual(LeadReminder.objects.get(lead=lead).title, "New follow-up")



class CallIntelligenceDashboardTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(package="enterprise", name="Call Dashboard Org")
        self.user = User.objects.create_user(
            email="call-dashboard@example.com",
            name="Call Admin",
            organization=self.org,
            role="admin",
            password="secret123",
        )
        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client = Client()
        self.client.cookies[get_session_cookie_name("dashboard")] = session.session_key

    def test_dashboard_renders_operational_call_intelligence_ui(self):
        response = self.client.get("/dashboard/call-intelligence/")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertContains(response, "CALL INTELLIGENCE")
        self.assertContains(response, "Download for Android")
        self.assertNotContains(response, "Calls at a glance.")
        analytics = self.client.get("/dashboard/call-intelligence/?section=analytics")
        self.assertContains(analytics, "MISSED-CALL RECOVERY")
        self.assertContains(analytics, "AGENT INTELLIGENCE")
        self.assertNotContains(analytics, "Download for Android")
        self.assertContains(response, "Custom dispositions")
