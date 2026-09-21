import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.crm.models import Lead, Pipeline
from apps.organizations.models import Organization
from apps.telephony.models import (
    CallDevice,
    CallEvent,
    CallIntelligenceSettings,
    CallRecord,
)
from apps.telephony.services import ingest_call_event, register_device


class CallIntelligenceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Call Intelligence Org")
        self.other_org = Organization.objects.create(name="Other Org")
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
        self.pipeline.country_code = "+91"
        self.pipeline.save(update_fields=["country_code", "updated_at"])
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
        self.org = Organization.objects.create(name="API Call Org")
        self.user = User.objects.create_user(
            email="api-agent@example.com",
            name="API Agent",
            organization=self.org,
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
