from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.messages.storage.fallback import FallbackStorage
from django.http import Http404
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.template.loader import get_template
from django.urls import reverse

from apps.accounts.models import User
from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.channels.template_delivery_ui import _revision, template_delivery_setup
from apps.channels.template_models import WhatsAppTemplateMetadata
from apps.organizations.models import Organization
from services.channels.campaign_policy import CampaignInputError
from services.channels.template_rendering import delivery_spec, render_for_lead, validate_delivery_bindings


class DeliveryBindingValidationTests(SimpleTestCase):
    def setUp(self):
        self.spec = {"components": [{"type": "HEADER", "format": "IMAGE"}, {"type": "BODY", "text": "Hello {{1}}"}]}

    def test_explicit_positional_mapping_and_fixed_fallback_are_supported(self):
        result = validate_delivery_bindings(self.spec, {"body.1": {"source": "lead_name", "default": "Customer"}}, {"lead_name"})
        self.assertEqual(result, {"body.1": {"source": "lead_name", "default": "Customer"}})

    def test_media_foreign_fields_unavailable_sources_and_object_values_are_rejected(self):
        cases = [
            {"header.media": {"default": "asset:foreign"}},
            {"body.2": {"source": "lead_name"}},
            {"body.1": {"source": "private_notes"}},
            {"body.1": {"default": {"text": "value"}}},
            {"body.1": {"source": "", "default": ""}},
            {"body.1": {"default": "x" * 2049}},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(CampaignInputError):
                validate_delivery_bindings(self.spec, case, {"lead_name"})

    def test_saved_positional_mapping_controls_actual_parameters_and_body(self):
        state = SimpleNamespace(
            components=[{"type": "BODY", "text": "Hi {{1}}, visit {{2}}"}],
            placeholder_mapping={}, delivery_media={},
            delivery_bindings={"body.1": {"source": "lead_name", "default": "Customer"}, "body.2": {"source": "", "default": "our website"}},
        )
        lead = SimpleNamespace(name="Asha", attributes={}, phone="+919876543210", email="", organization=SimpleNamespace(name="Org"), pipeline_id=None, stage_id=None)
        with patch("services.channels.template_rendering.state_for", return_value=state), patch("services.channels.template_rendering.available_placeholders", return_value=[{"key": "lead_name"}]):
            result = render_for_lead(template=SimpleNamespace(buttons=[]), lead=lead)
        self.assertEqual(result["body_text"], "Hi Asha, visit our website")
        self.assertEqual(result["components"][0]["parameters"], [{"type": "text", "text": "Asha"}, {"type": "text", "text": "our website"}])

    def test_setup_template_and_route_compile(self):
        self.assertIsNotNone(get_template("channels/whatsapp_template_delivery_setup.html"))
        self.assertIn("/sending/", reverse("whatsapp-template-delivery-setup", kwargs={"template_id": "00000000-0000-0000-0000-000000000001"}))


class TemplateDeliverySetupPermissionsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Sending setup")
        self.other_org = Organization.objects.create(name="Another organization")
        self.admin = User.objects.create_user(email="sending-admin@example.com", organization=self.org, role=User.Role.ADMIN)
        self.agent = User.objects.create_user(email="sending-agent@example.com", organization=self.org, role=User.Role.AGENT)
        self.account = WhatsAppAccount.objects.create(organization=self.org, connection_type="api", status="connected", is_active=True, phone_number_id="test-phone")
        self.template = WhatsAppTemplate.objects.create(organization=self.org, account=self.account, name="synced_offer", body="Hi {{1}}", status="approved", meta_template_id="test-template")
        self.state = WhatsAppTemplateMetadata.objects.create(template=self.template, components=[{"type": "BODY", "text": "Hi {{1}}"}])
        self.factory = RequestFactory()

    def request(self, user, data=None):
        request = self.factory.post("/sending/", data or {}) if data is not None else self.factory.get("/sending/")
        request.crm_user = request.user = user
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def invoke(self, request, template=None):
        return template_delivery_setup.__wrapped__(request, template_id=(template or self.template).pk)

    def post_data(self):
        return {"revision": _revision(delivery_spec(self.template, self.state), self.state), "source:body.1": "lead_name", "default:body.1": "Customer"}

    def test_admin_can_save_persistent_text_binding(self):
        response = self.invoke(self.request(self.admin, self.post_data()))
        self.assertEqual(response.status_code, 302)
        self.state.refresh_from_db()
        self.assertEqual(self.state.delivery_bindings, {"body.1": {"source": "lead_name", "default": "Customer"}})

    def test_agent_cannot_read_or_write_shared_template_defaults(self):
        for data in (None, self.post_data()):
            self.assertEqual(self.invoke(self.request(self.agent, data)).status_code, 403)
        self.state.refresh_from_db()
        self.assertEqual(self.state.delivery_bindings, {})

    def test_other_organization_template_is_inaccessible(self):
        other = User.objects.create_user(email="foreign-admin@example.com", organization=self.other_org, role=User.Role.ADMIN)
        with self.assertRaises(Http404):
            self.invoke(self.request(other, self.post_data()))

    def test_stale_revision_is_rejected_without_replacing_saved_defaults(self):
        data = self.post_data()
        self.state.delivery_bindings = {"body.1": {"source": "", "default": "Existing"}}
        self.state.save()
        self.assertEqual(self.invoke(self.request(self.admin, data)).status_code, 400)
        self.state.refresh_from_db()
        self.assertEqual(self.state.delivery_bindings["body.1"]["default"], "Existing")
