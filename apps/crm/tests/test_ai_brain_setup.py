from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
from django.http import HttpResponse
from django.template import Context, Engine
from django.test import RequestFactory, SimpleTestCase, TestCase

from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.ai_brain_setup import save_ai_brain_configuration
from apps.ai_engagement.services.knowledge_source import KnowledgeSourceServiceError
from apps.ai_engagement.services.org_info import OrgInfoServiceError
from apps.crm.views.ai_setup import _render_ai_setup, ai_setup_view
from apps.organizations.models import Organization


class AIBrainViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.organization = SimpleNamespace(id="our-organization", name="Example")
        self.user = SimpleNamespace(organization=self.organization)
        self.auth = patch(
            "apps.crm.authentication.get_crm_authenticated_user", return_value=self.user,
        )
        self.auth.start()
        self.addCleanup(self.auth.stop)
        self.message_patch = patch("apps.crm.views.ai_setup.messages")
        self.message_patch.start()
        self.addCleanup(self.message_patch.stop)

    def payload(self, **overrides):
        return {
            "action": "save_settings",
            "organization_name": "Example Business",
            "about": "We help teams schedule appointments.",
            "bot_languages": "English, Hindi",
            "ai_playbook": "##Rules\nUse only our approved information.",
            **overrides,
        }

    @patch("apps.crm.views.ai_setup.save_ai_brain_configuration")
    def test_save_uses_playbook_and_authenticated_tenant_without_resetting_controls(self, save):
        response = ai_setup_view(self.factory.post("/", self.payload(
            organization_id="another-organization",
            qualification_requirements="Obsolete instructions",
            engagement_instructions="Obsolete instructions",
        )))
        self.assertEqual(response.status_code, 302)
        values = save.call_args.kwargs
        self.assertIs(values["organization"], self.organization)
        self.assertEqual(set(values["data"]), {
            "organization_name", "about", "bot_languages", "ai_playbook",
        })
        self.assertEqual(values["data"]["ai_playbook"], self.payload()["ai_playbook"])

    @patch("apps.crm.views.ai_setup._render_ai_setup", return_value=HttpResponse(status=400))
    @patch("apps.crm.views.ai_setup.save_ai_brain_configuration")
    def test_invalid_playbook_retains_the_submitted_draft(self, save, render):
        save.side_effect = OrgInfoServiceError("The playbook needs a valid question.")
        response = ai_setup_view(self.factory.post("/", self.payload()))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(render.call_args.kwargs["form_values"]["ai_playbook"], self.payload()["ai_playbook"])

    @patch("apps.crm.views.ai_setup._render_ai_setup", return_value=HttpResponse(status=400))
    @patch("apps.crm.views.ai_setup.save_ai_brain_configuration")
    def test_blank_company_description_is_rejected_before_any_write(self, save, render):
        response = ai_setup_view(self.factory.post("/", self.payload(about="  ")))
        self.assertEqual(response.status_code, 400)
        save.assert_not_called()

    @patch("apps.crm.views.ai_setup.render")
    @patch("apps.crm.views.ai_setup._get_knowledge_data", return_value=(Mock(), Mock()))
    @patch("apps.crm.views.ai_setup.OrgInfoService")
    def test_questionnaire_without_criteria_shows_admin_guidance(self, org_service, knowledge, render):
        request = self.factory.get("/")
        for criteria, expected in (("", True), ("All required questions are answered.", False)):
            with self.subTest(criteria=criteria):
                _render_ai_setup(request, self.organization, form_values={
                    "ai_playbook": "##Qualification Questions\nWhat is your budget?\n##Qualification Criteria\n" + criteria,
                })
                self.assertEqual(render.call_args.args[2]["playbook_needs_criteria"], expected)


class AIBrainConfigurationTests(SimpleTestCase):
    @patch("apps.ai_engagement.services.ai_brain_setup.transaction")
    @patch("apps.ai_engagement.services.ai_brain_setup.KnowledgeSourceService")
    @patch("apps.ai_engagement.services.ai_brain_setup.OrgInfoService")
    @patch("apps.ai_engagement.services.ai_brain_setup.ingest_and_index_url_source")
    def test_sources_are_tenant_scoped_deduplicated_and_queued_after_commit(
        self, ingest, org_service, source_service, transaction,
    ):
        organization = SimpleNamespace(id="tenant-a")
        source_service.return_value.create_url_source.return_value = SimpleNamespace(id=123)
        save_ai_brain_configuration(
            organization=organization,
            data={"ai_playbook": "##Rules\nBe concise."},
            urls=[" https://example.com ", "https://example.com", ""],
        )
        source_service.return_value.create_url_source.assert_called_once_with(
            organization=organization, url="https://example.com",
        )
        ingest.delay.assert_not_called()
        transaction.on_commit.call_args.args[0]()
        ingest.delay.assert_called_once_with(source_id=123, organization_id="tenant-a")

    @patch("apps.ai_engagement.services.ai_brain_setup.OrgInfoService")
    def test_excess_urls_are_rejected_before_config_is_changed(self, org_service):
        with self.assertRaisesMessage(KnowledgeSourceServiceError, "up to 10"):
            save_ai_brain_configuration(
                organization=Mock(), data={},
                urls=[f"https://example.com/{number}" for number in range(11)],
            )
        org_service.assert_not_called()

    @patch("apps.ai_engagement.services.ai_brain_setup.transaction")
    @patch("apps.ai_engagement.services.ai_brain_setup.KnowledgeSourceService")
    @patch("apps.ai_engagement.services.ai_brain_setup.OrgInfoService")
    def test_invalid_url_cannot_store_uploaded_file(self, org_service, source_service, transaction):
        source_service.return_value.create_url_source.side_effect = KnowledgeSourceServiceError("Unsafe URL")
        with self.assertRaises(KnowledgeSourceServiceError):
            save_ai_brain_configuration(
                organization=Mock(), data={}, urls=["http://localhost"], uploaded_file=Mock(),
            )
        source_service.return_value.create_file_source.assert_not_called()
        transaction.on_commit.assert_not_called()


class AIBrainTemplateTests(SimpleTestCase):
    def test_single_playbook_source_controls_and_existing_sandbox_are_rendered(self):
        engine = Engine(
            dirs=[settings.BASE_DIR / "templates"],
            libraries={"static": "django.templatetags.static"},
            loaders=[
                ("django.template.loaders.locmem.Loader", {
                    "base.html": "{% block extra_head %}{% endblock %}{% block content %}{% endblock %}",
                }),
                "django.template.loaders.filesystem.Loader",
            ],
        )
        html = engine.get_template("crm/knowledge_base/ai_setup.html").render(Context({
            "form_values": {"organization_name": "Example", "ai_playbook": "<script>bad()</script>"},
            "pending_urls": ["https://example.com"],
            "supported_file_extensions": [".txt", ".pdf"],
            "knowledge_max_upload_bytes": 10 * 1024 * 1024,
        }))
        self.assertIn('name="ai_playbook"', html)
        self.assertIn("&lt;script&gt;bad()&lt;/script&gt;", html)
        self.assertNotIn('name="qualification_requirements"', html)
        self.assertNotIn('name="engagement_instructions"', html)
        self.assertNotIn('name="bump_up_enabled"', html)
        for field in ("organization_name", "about", "bot_languages", "knowledge_urls", "knowledge_file", "share_instruction"):
            self.assertIn(f'name="{field}"', html)
        for control in ("playground-messages", "playground-message-input", "playground-send-message", "playground-restart-chat"):
            self.assertIn(f'id="{control}"', html)


class AIBrainPersistenceTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Original Name")
        self.info = OrgInfo.objects.create(
            organization=self.organization,
            about="Original description",
            ai_playbook="##Rules\nOriginal rule.",
            ai_enabled=False,
            bump_up_enabled=True,
            bump_up_count=4,
        )
        self.data = {
            "organization_name": "Updated Name",
            "about": "Updated description",
            "bot_languages": "English",
            "ai_playbook": "##Rules\nUse approved information.",
        }

    def test_profile_and_playbook_save_without_changing_hidden_runtime_controls(self):
        save_ai_brain_configuration(organization=self.organization, data=self.data)
        self.organization.refresh_from_db()
        self.info.refresh_from_db()
        self.assertEqual(self.organization.name, "Updated Name")
        self.assertEqual(self.info.ai_playbook, self.data["ai_playbook"])
        self.assertFalse(self.info.ai_enabled)
        self.assertTrue(self.info.bump_up_enabled)
        self.assertEqual(self.info.bump_up_count, 4)

    @patch('apps.crm.views.ai_setup.messages.success')
    def test_save_and_reload_json_confirms_database_content(self, success):
        user = SimpleNamespace(organization=self.organization)
        request = RequestFactory().post('/', {**self.data, 'action': 'save_settings'}, HTTP_ACCEPT='application/json')
        with patch('apps.crm.authentication.get_crm_authenticated_user', return_value=user):
            response = ai_setup_view(request)
        import json
        result = json.loads(response.content)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(result['saved'])
        self.info.refresh_from_db()
        self.assertEqual(result['ai_playbook'], self.info.ai_playbook)
        self.assertEqual(self.info.ai_playbook, self.data['ai_playbook'])
        success.assert_called_once()

    def test_unrelated_legacy_organization_field_does_not_block_playbook_save(self):
        Organization.objects.filter(pk=self.organization.pk).update(payment_mode='legacy')
        self.organization.refresh_from_db()
        save_ai_brain_configuration(organization=self.organization, data=self.data)
        self.info.refresh_from_db()
        self.assertEqual(self.info.ai_playbook, self.data['ai_playbook'])

    def test_json_validation_failure_never_reports_saved(self):
        request = RequestFactory().post('/', {**self.data, 'organization_name': '', 'action': 'save_settings'}, HTTP_ACCEPT='application/json')
        with patch('apps.crm.authentication.get_crm_authenticated_user', return_value=SimpleNamespace(organization=self.organization)):
            response = ai_setup_view(request)
        import json
        self.assertEqual(response.status_code, 400)
        self.assertFalse(json.loads(response.content)['saved'])
        self.info.refresh_from_db()
        self.assertNotEqual(self.info.ai_playbook, self.data['ai_playbook'])

    @patch("apps.ai_engagement.services.ai_brain_setup.KnowledgeSourceService")
    def test_invalid_source_rolls_back_the_entire_configuration_save(self, sources):
        sources.return_value.create_url_source.side_effect = KnowledgeSourceServiceError("Invalid website URL")
        with self.assertRaises(KnowledgeSourceServiceError):
            save_ai_brain_configuration(
                organization=self.organization, data=self.data, urls=["http://localhost"],
            )
        self.organization.refresh_from_db()
        self.info.refresh_from_db()
        self.assertEqual(self.organization.name, "Original Name")
        self.assertEqual(self.info.about, "Original description")
        self.assertEqual(self.info.ai_playbook, "##Rules\nOriginal rule.")
