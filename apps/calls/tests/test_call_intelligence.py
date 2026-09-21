import uuid

from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.accounts.models import User
from apps.calls.models import CallRecord
from apps.crm.models import LeadReminder, Pipeline, Stage
from apps.organizations.models import Organization


class CallIntelligenceAPITests(APITestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Call Intelligence Test")
        self.user = User.objects.create_user(
            email="agent-call@example.com",
            password="test-pass",
            organization=self.org,
            name="Call Agent",
            role=User.Role.AGENT,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.org,
            name="Leads",
            owner=self.user,
            is_active=True,
        )
        self.stage = Stage.objects.create(
            pipeline=self.pipeline,
            name="New Lead",
            display_order=0,
            is_active=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.device_id = uuid.uuid4()

    def register_device(self):
        response = self.client.post(
            "/api/v1/call-intelligence/devices/register/",
            {
                "device_id": str(self.device_id),
                "device_name": "Pixel Test",
                "manufacturer": "Google",
                "model": "Pixel",
                "android_version": "14",
                "app_version": "1.0.0",
                "permissions": {"READ_CALL_LOG": True},
                "battery_optimization_ignored": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)

    def test_terminal_call_is_idempotent_and_auto_creates_lead(self):
        self.register_device()
        source_call_id = "android-call-001"
        event_uuid = uuid.uuid4()
        payload = {
            "device_id": str(self.device_id),
            "event_uuid": str(event_uuid),
            "source_call_id": source_call_id,
            "event_type": "completed",
            "direction": "outbound",
            "status": "completed",
            "phone_number": "9876543210",
            "contact_name": "Rahul",
            "occurred_at": timezone.now().isoformat(),
            "started_at": timezone.now().isoformat(),
            "duration_seconds": 120,
        }

        first = self.client.post(
            "/api/v1/call-intelligence/events/",
            payload,
            format="json",
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(CallRecord.objects.count(), 1)
        call = CallRecord.objects.get()
        self.assertEqual(call.phone_number, "+919876543210")
        self.assertIsNotNone(call.lead_id)
        self.assertEqual(call.lead.lead_source, "call_intelligence")
        self.assertIsNotNone(call.crm_call_id)

        second = self.client.post(
            "/api/v1/call-intelligence/events/",
            payload,
            format="json",
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(CallRecord.objects.count(), 1)
        self.assertEqual(call.events.count(), 1)

    def test_agent_cannot_see_another_users_calls(self):
        self.register_device()
        other = User.objects.create_user(
            email="other-call@example.com",
            password="test-pass",
            organization=self.org,
            name="Other Agent",
            role=User.Role.AGENT,
        )
        other_pipeline = Pipeline.objects.create(
            organization=self.org,
            name="Other",
            owner=other,
            is_active=True,
        )
        Stage.objects.create(
            pipeline=other_pipeline,
            name="New Lead",
            display_order=0,
            is_active=True,
        )
        CallRecord.objects.create(
            organization=self.org,
            user=other,
            source_call_id="other-1",
            phone_number="+919999999999",
            called_at=timezone.now(),
        )

        response = self.client.get("/api/v1/call-intelligence/calls/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)


    def test_same_call_can_receive_post_call_notes_and_follow_up(self):
        self.register_device()
        source_call_id = "android-call-enrichment"
        first = {
            "device_id": str(self.device_id),
            "event_uuid": str(uuid.uuid4()),
            "source_call_id": source_call_id,
            "event_type": "completed",
            "direction": "inbound",
            "status": "completed",
            "phone_number": "9888888888",
            "contact_name": "Post Call Lead",
            "occurred_at": timezone.now().isoformat(),
            "duration_seconds": 90,
        }
        response = self.client.post(
            "/api/v1/call-intelligence/events/",
            first,
            format="json",
        )
        self.assertEqual(response.status_code, 201)

        follow_up = timezone.now() + timedelta(days=1)
        second = {
            **first,
            "event_uuid": str(uuid.uuid4()),
            "notes": "Customer asked for a product demo.",
            "disposition": "demo_requested",
            "follow_up_at": follow_up.isoformat(),
        }
        with self.captureOnCommitCallbacks(execute=False):
            response = self.client.post(
                "/api/v1/call-intelligence/events/",
                second,
                format="json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CallRecord.objects.count(), 1)

        call = CallRecord.objects.get()
        self.assertEqual(call.notes, "Customer asked for a product demo.")
        self.assertEqual(call.disposition, "demo_requested")
        self.assertEqual(call.crm_call.notes, call.notes)
        self.assertEqual(LeadReminder.objects.filter(lead=call.lead).count(), 1)
