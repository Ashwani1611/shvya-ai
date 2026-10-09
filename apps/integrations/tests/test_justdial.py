from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import get_session_cookie_name, set_authenticated_user
from apps.crm.models import Lead, Pipeline, Stage
from apps.integrations.justdial_models import JustDialIntegration, JustDialLeadEvent
from apps.organizations.models import Organization


class JustDialIntegrationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="JustDial Test Org")
        self.admin = User.objects.create_user(
            email="justdial-admin@example.test",
            password="test-password",
            name="JustDial Admin",
            organization=self.organization,
            role=User.Role.ADMIN,
        )
        self.superadmin = User.objects.create_superuser(
            email="justdial-superadmin@example.test",
            password=None,
            name="JustDial Superadmin",
        )
        self.pipeline = (
            Pipeline.objects.filter(
                organization=self.organization,
                is_active=True,
            ).first()
            or Pipeline.objects.create(
                organization=self.organization,
                name="Sales",
            )
        )
        self.stage = self.pipeline.stages.filter(is_active=True).order_by(
            "display_order"
        ).first()
        if self.stage is None:
            self.stage = Stage.objects.create(
                pipeline=self.pipeline,
                name="New Lead",
                display_order=1,
            )

    def authenticate_dashboard(self):
        session = SessionStore()
        set_authenticated_user(session, self.admin)
        session.save()
        self.client.cookies[get_session_cookie_name("dashboard")] = session.session_key

    def authenticate_superadmin(self):
        session = SessionStore()
        set_authenticated_user(session, self.superadmin)
        session.save()
        self.client.cookies[get_session_cookie_name("superadmin")] = session.session_key

    def provision(self):
        integration = JustDialIntegration.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
        )
        integration.generate_webhook_token()
        integration.is_enabled = True
        integration.full_clean()
        integration.save()
        return integration

    def test_org_admin_can_request_setup_but_cannot_generate_webhook(self):
        self.authenticate_dashboard()

        response = self.client.post(
            reverse("crm-connect-hub-justdial"),
            {"action": "request_setup"},
        )
        self.assertEqual(response.status_code, 302)

        integration = JustDialIntegration.objects.get(
            organization=self.organization
        )
        self.assertIsNone(integration.webhook_token)
        self.assertFalse(integration.is_enabled)

        response = self.client.post(
            reverse("crm-connect-hub-justdial"),
            {"action": "generate"},
        )
        self.assertEqual(response.status_code, 400)
        integration.refresh_from_db()
        self.assertIsNone(integration.webhook_token)

    def test_superadmin_generates_org_scoped_webhook_and_routing(self):
        JustDialIntegration.objects.create(organization=self.organization)
        self.authenticate_superadmin()

        response = self.client.post(
            reverse(
                "superadmin-organization-justdial",
                kwargs={"organization_id": self.organization.pk},
            ),
            {
                "action": "generate",
                "pipeline_id": str(self.pipeline.pk),
                "stage_id": str(self.stage.pk),
            },
        )
        self.assertEqual(response.status_code, 302)

        integration = JustDialIntegration.objects.get(
            organization=self.organization
        )
        self.assertIsNotNone(integration.webhook_token)
        self.assertTrue(integration.is_enabled)
        self.assertEqual(integration.pipeline_id, self.pipeline.id)
        self.assertEqual(integration.stage_id, self.stage.id)

        self.client.cookies.pop(get_session_cookie_name("superadmin"), None)
        self.authenticate_dashboard()
        page = self.client.get(reverse("crm-connect-hub-justdial"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, str(integration.webhook_token))
        self.assertContains(page, "GET")

    @patch("services.crm.lead_service._schedule_new_lead_welcome")
    def test_get_webhook_creates_justdial_lead_without_auto_welcome(self, welcome):
        integration = self.provision()
        url = reverse(
            "justdial-webhook",
            kwargs={"token": integration.webhook_token},
        )

        response = self.client.get(
            url,
            {
                "leadid": "JD-10001",
                "name": "Rahul Sharma",
                "mobile": "9876543210",
                "email": "rahul@example.com",
                "category": "CRM Software",
                "city": "Delhi",
                "area": "Rohini",
                "leadtype": "Hot",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"SUCCESS")
        lead = Lead.objects.get(
            organization=self.organization,
            phone="+919876543210",
        )
        self.assertEqual(lead.name, "Rahul Sharma")
        self.assertEqual(lead.lead_source, "justdial")
        self.assertEqual(lead.pipeline_id, self.pipeline.id)
        self.assertEqual(lead.stage_id, self.stage.id)
        self.assertEqual(lead.attributes["justdial_lead_id"], "JD-10001")
        self.assertEqual(lead.attributes["justdial_category"], "CRM Software")
        self.assertEqual(lead.attributes["justdial_city"], "Delhi")
        welcome.assert_not_called()

        event = JustDialLeadEvent.objects.get(integration=integration)
        self.assertEqual(event.status, JustDialLeadEvent.Status.CREATED)
        self.assertEqual(event.external_lead_id, "JD-10001")

    def test_repeated_phone_updates_same_lead_and_records_updated_event(self):
        integration = self.provision()
        url = reverse(
            "justdial-webhook",
            kwargs={"token": integration.webhook_token},
        )

        first = self.client.get(
            url,
            {
                "leadid": "JD-20001",
                "name": "First Name",
                "mobile": "919999999999",
                "city": "Noida",
            },
        )
        second = self.client.get(
            url,
            {
                "leadid": "JD-20002",
                "name": "Updated Name",
                "mobile": "+91 99999 99999",
                "city": "Delhi",
            },
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.content, b"SUCCESS")
        self.assertEqual(
            Lead.objects.filter(organization=self.organization).count(),
            1,
        )
        lead = Lead.objects.get(organization=self.organization)
        self.assertEqual(lead.name, "Updated Name")
        self.assertEqual(lead.attributes["justdial_lead_id"], "JD-20002")
        self.assertEqual(lead.attributes["justdial_city"], "Delhi")
        self.assertEqual(
            JustDialLeadEvent.objects.filter(
                integration=integration,
                status=JustDialLeadEvent.Status.UPDATED,
            ).count(),
            1,
        )

    def test_json_post_is_supported_for_provider_compatibility(self):
        integration = self.provision()
        url = reverse(
            "justdial-webhook",
            kwargs={"token": integration.webhook_token},
        )

        response = self.client.post(
            url,
            data={
                "lead_id": "JD-JSON-1",
                "full_name": "JSON Lead",
                "mobile_number": "9811111111",
                "category_name": "Training",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"SUCCESS")
        self.assertTrue(
            Lead.objects.filter(
                organization=self.organization,
                phone="+919811111111",
                lead_source="justdial",
            ).exists()
        )

    def test_bad_email_does_not_drop_valid_phone_lead(self):
        integration = self.provision()
        url = reverse(
            "justdial-webhook",
            kwargs={"token": integration.webhook_token},
        )

        response = self.client.get(
            url,
            {
                "leadid": "JD-BAD-EMAIL",
                "name": "Phone Valid",
                "mobile": "9833333333",
                "email": "not-an-email",
            },
        )

        self.assertEqual(response.status_code, 200)
        lead = Lead.objects.get(
            organization=self.organization,
            phone="+919833333333",
        )
        self.assertEqual(lead.email, "")

    def test_provider_secrets_are_redacted_from_event_log(self):
        integration = self.provision()
        url = reverse(
            "justdial-webhook",
            kwargs={"token": integration.webhook_token},
        )

        response = self.client.get(
            url,
            {
                "leadid": "JD-SECRET-1",
                "name": "Secret Safe Lead",
                "mobile": "9822222222",
                "token": "provider-token",
                "username": "provider-user",
                "password": "provider-password",
                "client_key": "provider-client-key",
            },
        )

        self.assertEqual(response.status_code, 200)
        event = JustDialLeadEvent.objects.get(
            integration=integration,
            external_lead_id="JD-SECRET-1",
        )
        self.assertEqual(event.payload["token"], "[REDACTED]")
        self.assertEqual(event.payload["username"], "[REDACTED]")
        self.assertEqual(event.payload["password"], "[REDACTED]")
        self.assertEqual(event.payload["client_key"], "[REDACTED]")
        serialized = str(event.payload)
        self.assertNotIn("provider-token", serialized)
        self.assertNotIn("provider-password", serialized)
        self.assertNotIn("provider-client-key", serialized)

    def test_invalid_phone_is_logged_and_rejected(self):
        integration = self.provision()
        url = reverse(
            "justdial-webhook",
            kwargs={"token": integration.webhook_token},
        )

        response = self.client.get(
            url,
            {"leadid": "JD-BAD", "name": "No Phone"},
        )

        self.assertEqual(response.status_code, 422)
        event = JustDialLeadEvent.objects.get(integration=integration)
        self.assertEqual(event.status, JustDialLeadEvent.Status.FAILED)
        self.assertIn("phone", event.error_message.lower())

    def test_unknown_webhook_token_is_not_discoverable(self):
        from uuid import uuid4

        response = self.client.get(
            reverse("justdial-webhook", kwargs={"token": uuid4()}),
            {"mobile": "9876543210"},
        )
        self.assertEqual(response.status_code, 404)

    def test_justdial_postman_contract_creates_lead_with_complete_fields(self):
        """Exercise the provider POST schema using synthetic contact details."""
        integration = self.provision()
        url = reverse(
            "justdial-webhook",
            kwargs={"token": integration.webhook_token},
        )
        payload = {
            "leadid": "JD-SYNTHETIC-12345",
            "leadtype": "category",
            "prefix": "",
            "name": "Messaging",
            "mobile": "9876543210",
            "phone": "",
            "email": "jd-test@example.com",
            "date": "2026-10-08",
            "category": "Generator Dealer",
            "area": "Ghatkopar West",
            "city": "Mumbai",
            "brancharea": "Apollo Bunder",
            "dncmobile": 0,
            "dncphone": 0,
            "company": "Example Generator Dealers",
            "pincode": "0",
            "time": "13:10:11",
            "branchpin": "400001",
            "parentid": "PK-DEMO-123",
            "state": "Maharashtra",
        }
        with patch("services.crm.lead_service._schedule_new_lead_welcome") as welcome:
            response = self.client.post(url, payload, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"SUCCESS")
        lead = Lead.objects.get(organization=self.organization, phone="+919876543210")
        self.assertEqual(lead.name, "Messaging")
        self.assertEqual(lead.email, "jd-test@example.com")
        self.assertEqual(lead.lead_source, "justdial")
        self.assertEqual(lead.pipeline_id, self.pipeline.pk)
        self.assertEqual(lead.stage_id, self.stage.pk)
        for key, value in {
            "justdial_lead_id": "JD-SYNTHETIC-12345",
            "justdial_lead_type": "category",
            "justdial_category": "Generator Dealer",
            "justdial_area": "Ghatkopar West",
            "justdial_city": "Mumbai",
            "justdial_state": "Maharashtra",
            "justdial_branch_area": "Apollo Bunder",
            "justdial_company": "Example Generator Dealers",
            "justdial_pincode": "0",
            "justdial_inquiry_date": "2026-10-08",
            "justdial_inquiry_time": "13:10:11",
            "justdial_parent_id": "PK-DEMO-123",
            "justdial_branch_pin": "400001",
            "justdial_dnc_mobile": "0",
            "justdial_dnc_phone": "0",
        }.items():
            self.assertEqual(lead.attributes[key], value, key)
        welcome.assert_not_called()
        event = JustDialLeadEvent.objects.get(integration=integration)
        self.assertEqual(event.method, "POST")
        self.assertEqual(event.status, JustDialLeadEvent.Status.CREATED)

    def test_duplicate_leadid_acknowledged_without_reverting_qualified_lead(self):
        integration = self.provision()
        url = reverse("justdial-webhook", kwargs={"token": integration.webhook_token})
        payload = {
            "leadid": "JD-RETRY-1",
            "name": "Asha",
            "mobile": "9812345678",
            "city": "Mumbai",
        }
        first = self.client.post(url, payload, content_type="application/json")
        self.assertEqual(first.content, b"SUCCESS")
        lead = Lead.objects.get(organization=self.organization, phone="+919812345678")
        qualified = self.pipeline.stages.filter(name="Qualified").first()
        if qualified is None:
            qualified = Stage.objects.create(
                pipeline=self.pipeline,
                name="Qualified",
                display_order=99,
            )
        lead.stage = qualified
        lead.save()

        second = self.client.post(
            url,
            {**payload, "name": "Different Name", "city": "Delhi"},
            content_type="application/json",
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.content, b"SUCCESS")
        lead.refresh_from_db()
        self.assertEqual(lead.name, "Asha")
        self.assertEqual(lead.stage_id, qualified.id)
        self.assertEqual(lead.attributes["justdial_city"], "Mumbai")
        self.assertEqual(Lead.objects.filter(organization=self.organization).count(), 1)
        self.assertEqual(
            JustDialLeadEvent.objects.filter(
                integration=integration, status=JustDialLeadEvent.Status.IGNORED
            ).count(),
            1,
        )

    def test_new_enquiry_for_existing_contact_preserves_stage_and_original_name(self):
        integration = self.provision()
        later = self.pipeline.stages.filter(name="Qualified").first()
        if later is None:
            later = Stage.objects.create(
                pipeline=self.pipeline, name="Qualified", display_order=99
            )
        lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=later,
            name="Customer-provided name",
            phone="+919876543210",
            email="existing@example.com",
            lead_source="external_api",
        )
        url = reverse("justdial-webhook", kwargs={"token": integration.webhook_token})
        response = self.client.post(
            url,
            {
                "leadid": "JD-NEW-ENQUIRY",
                "mobile": "9876543210",
                "name": "Messaging",
                "email": "justdial@example.com",
                "state": "Delhi",
            },
            content_type="application/json",
        )
        self.assertEqual(response.content, b"SUCCESS")
        lead.refresh_from_db()
        self.assertEqual(lead.stage_id, later.id)
        self.assertEqual(lead.name, "Customer-provided name")
        self.assertEqual(lead.email, "existing@example.com")
        self.assertEqual(lead.lead_source, "external_api")
        self.assertEqual(lead.attributes["justdial_state"], "Delhi")

    def test_post_uses_fallback_phone_when_mobile_is_missing_or_invalid(self):
        integration = self.provision()
        url = reverse("justdial-webhook", kwargs={"token": integration.webhook_token})
        response = self.client.post(
            url,
            {
                "leadid": "JD-PHONE-FALLBACK",
                "name": "Fallback",
                "mobile": "not-a-number",
                "phone": "9876543210",
            },
            content_type="application/json",
        )
        self.assertEqual(response.content, b"SUCCESS")
        self.assertTrue(
            Lead.objects.filter(
                organization=self.organization, phone="+919876543210"
            ).exists()
        )

    def test_oversized_post_is_rejected_without_creating_lead(self):
        integration = self.provision()
        url = reverse("justdial-webhook", kwargs={"token": integration.webhook_token})
        response = self.client.post(
            url,
            {"leadid": "JD-TOO-LARGE", "mobile": "9876543210", "extra": "x" * 263000},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 413)
        self.assertFalse(Lead.objects.filter(organization=self.organization).exists())

    def test_paused_connection_returns_retryable_status_not_success(self):
        integration = self.provision()
        url = reverse("justdial-webhook", kwargs={"token": integration.webhook_token})
        integration.is_enabled = False
        integration.save(update_fields=["is_enabled"])
        response = self.client.post(
            url,
            {"leadid": "JD-PAUSED", "mobile": "9876543210"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 503)
        self.assertNotEqual(response.content, b"SUCCESS")
        self.assertFalse(Lead.objects.filter(organization=self.organization).exists())
