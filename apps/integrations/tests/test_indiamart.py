import json
from unittest.mock import patch
from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from apps.accounts.models import User
from apps.accounts.session_utils import get_session_cookie_name, set_authenticated_user
from apps.crm.models import Lead, Pipeline, Stage
from apps.integrations.models import IndiaMartConnection, IndiaMartReceipt
from apps.organizations.models import Organization


class IndiaMartTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="IndiaMART test")
        self.pipeline = Pipeline.objects.create(organization=self.org, name="Sales")
        self.stage = Stage.objects.create(
            pipeline=self.pipeline, name="New Lead", display_order=0
        )
        self.connection = IndiaMartConnection.objects.create(
            organization=self.org,
            pipeline=self.pipeline,
            stage=self.stage,
            is_enabled=True,
            generated_at=timezone.now(),
        )
        self.url = reverse(
            "indiamart-ingest", kwargs={"token": self.connection.webhook_token}
        )
        self.row = {
            "UNIQUE_QUERY_ID": "123",
            "SENDER_NAME": "Buyer",
            "SENDER_MOBILE": "9876543210",
            "SENDER_COUNTRY_ISO": "IN",
            "QUERY_MESSAGE": "Need tiles",
        }
        self.welcome = patch(
            "apps.integrations.views.indiamart._schedule_new_lead_welcome"
        )
        self.welcome.start()
        self.addCleanup(self.welcome.stop)

    def authenticate(self, user, area):
        session = SessionStore()
        set_authenticated_user(session, user)
        session.create()
        self.client.cookies[get_session_cookie_name(area)] = session.session_key

    def post(self, row=None):
        return self.client.post(
            self.url,
            json.dumps({"CODE": 200, "RESPONSE": row or self.row}),
            content_type="application/json",
        )

    def test_push_and_duplicate(self):
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.post().status_code, 200)
        lead = Lead.objects.get(organization=self.org, phone="+919876543210")
        self.assertEqual(lead.lead_source, "indiamart")
        self.assertEqual(lead.stage, self.stage)
        self.assertIn("Need tiles", lead.notes)
        self.assertEqual(IndiaMartReceipt.objects.count(), 1)

    def test_repeat_buyer_keeps_stage_and_notes(self):
        self.post()
        lead = Lead.objects.get(organization=self.org, phone="+919876543210")
        later, _ = Stage.objects.get_or_create(
            pipeline=self.pipeline, name="Qualified", defaults={"display_order": 1}
        )
        lead.stage = later
        lead.save()
        self.assertEqual(
            self.post(
                {
                    **self.row,
                    "UNIQUE_QUERY_ID": "124",
                    "QUERY_MESSAGE": "Another requirement",
                }
            ).status_code,
            200,
        )
        lead.refresh_from_db()
        self.assertEqual(lead.stage, later)
        self.assertEqual(lead.lead_notes.count(), 1)
        self.assertIn("Need tiles", lead.notes)

    def test_invalid_batch_rolls_back(self):
        response = self.post([self.row, {"UNIQUE_QUERY_ID": "bad"}])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(IndiaMartReceipt.objects.exists())
        self.assertFalse(Lead.objects.filter(organization=self.org).exists())

    def test_disabled_unknown_and_oversized(self):
        self.connection.is_enabled = False
        self.connection.save()
        self.assertEqual(self.post().status_code, 404)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertEqual(
            self.client.post(
                self.url, "x" * 262145, content_type="application/json"
            ).status_code,
            413,
        )

    def test_request_does_not_enable_or_expose_webhook(self):
        self.connection.delete()
        admin = User.objects.create_user(
            email="admin@im.test",
            password="test",
            name="Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        self.authenticate(admin, "dashboard")
        url = reverse("crm-connect-hub-indiamart")
        self.assertEqual(self.client.post(url).status_code, 302)
        connection = IndiaMartConnection.objects.get(organization=self.org)
        self.assertIsNotNone(connection.requested_at)
        self.assertFalse(connection.is_enabled)
        self.assertIsNone(connection.generated_at)
        self.assertNotContains(self.client.get(url), str(connection.webhook_token))
        setup = reverse("superadmin-indiamart", kwargs={"organization_id": self.org.id})
        self.assertEqual(
            self.client.post(
                setup, {"action": "generate", "stage": self.stage.id}
            ).status_code,
            302,
        )
        connection.refresh_from_db()
        self.assertIsNone(connection.generated_at)

    def test_superadmin_generation_and_cross_tenant_routing(self):
        user = User.objects.create_superuser(
            email="super@im.test", password="test", name="Super"
        )
        self.authenticate(user, "superadmin")
        url = reverse("superadmin-indiamart", kwargs={"organization_id": self.org.id})
        other = Organization.objects.create(name="Other")
        pipeline = Pipeline.objects.create(organization=other, name="Other")
        stage = Stage.objects.create(pipeline=pipeline, name="New", display_order=0)
        self.assertEqual(
            self.client.post(
                url, {"action": "generate", "stage": stage.id}
            ).status_code,
            400,
        )
        old = self.connection.webhook_token
        self.assertEqual(
            self.client.post(
                url, {"action": "rotate", "stage": self.stage.id}
            ).status_code,
            302,
        )
        self.connection.refresh_from_db()
        self.assertNotEqual(old, self.connection.webhook_token)
        self.assertEqual(self.post().status_code, 404)
        response = self.client.get(url)
        self.assertContains(response, str(self.connection.webhook_token))
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_receiver_does_not_require_csrf(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            self.url, json.dumps(self.row), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)

    def test_same_phone_in_another_organization_is_isolated(self):
        other = Organization.objects.create(name="Another seller")
        pipeline = Pipeline.objects.create(organization=other, name="Other sales")
        stage = Stage.objects.create(
            pipeline=pipeline, name="New enquiry", display_order=0
        )
        lead = Lead.objects.create(
            organization=other,
            pipeline=pipeline,
            stage=stage,
            name="Other buyer",
            phone="+919876543210",
        )
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(Lead.objects.filter(phone="+919876543210").count(), 2)
        lead.refresh_from_db()
        self.assertEqual(lead.name, "Other buyer")
        self.assertEqual(lead.lead_notes.count(), 0)

    def test_malformed_payload_and_foreign_routing_fail_closed(self):
        self.assertEqual(
            self.client.post(
                self.url, "{", content_type="application/json"
            ).status_code,
            400,
        )
        self.assertEqual(
            self.post(
                {**self.row, "SENDER_MOBILE": "", "SENDER_PHONE": ""}
            ).status_code,
            400,
        )
        other = Organization.objects.create(name="Foreign routing")
        pipeline = Pipeline.objects.create(organization=other, name="Foreign")
        self.connection.pipeline = pipeline
        self.connection.save(update_fields=["pipeline"])
        self.assertEqual(self.post().status_code, 503)
        self.assertFalse(IndiaMartReceipt.objects.exists())

    def test_queue_failure_does_not_fail_saved_enquiry(self):
        with patch(
            "apps.integrations.views.indiamart._schedule_new_lead_welcome",
            side_effect=RuntimeError("Queue unavailable"),
        ):
            with self.assertLogs("django.test", level="ERROR"):
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IndiaMartReceipt.objects.count(), 1)
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(IndiaMartReceipt.objects.count(), 1)
