from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.template import Context, Engine
from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.ai_engagement.views.brain_bundle import (
    OrganizationAIBrainBundleView,
    dashboard_ai_brain_download,
)


class OrganizationAIBrainBundleExportTests(SimpleTestCase):
    def setUp(self):
        self.organization = SimpleNamespace(
            pk=uuid4(), name="An organization\r\nwith a header-like name", is_active=True,
        )
        self.user = self.author("admin")
        self.bundle = {
            "schema_version": 1,
            "revision": "ab" * 32,
            "generated_at": "2026-10-05T12:00:00Z",
            "organization": {"id": str(self.organization.pk)},
            "ai": {"about": "हम आपकी मदद करते हैं", "bot_languages": "Hindi"},
            "faqs": [{"question": "Which services?", "answer": "Training."}],
        }
        builder_patch = patch(
            "apps.ai_engagement.views.brain_bundle.get_organization_ai_brain_bundle",
            return_value=self.bundle,
        )
        self.builder = builder_patch.start()
        self.addCleanup(builder_patch.stop)

    def author(self, role, **overrides):
        values = {
            "is_authenticated": True, "is_active": True, "is_superuser": False,
            "organization": self.organization, "role": role,
        }
        return SimpleNamespace(**(values | overrides))

    def api_request(self, *, user=None, method="get", data=None):
        request = getattr(APIRequestFactory(), method)(
            reverse("ai-brain-bundle"), data or {}, format="json",
        )
        if user is not None:
            force_authenticate(request, user=user)
        return OrganizationAIBrainBundleView.as_view()(request)

    def assert_bundle_download(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), self.bundle)
        self.assertIn("हम आपकी मदद करते हैं", response.content.decode("utf-8"))
        self.assertEqual(response["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(
            response["Content-Disposition"],
            f'attachment; filename="shvya-ai-brain-{self.organization.pk}-abababababab.json"',
        )

    def test_dashboard_uses_only_dedicated_session_organization(self):
        other_organization = uuid4()
        request = RequestFactory().get(
            reverse("crm-ai-brain-download"), {"organization_id": str(other_organization)},
        )
        # A different ordinary Django login must never set CRM tenant scope.
        request.user = SimpleNamespace(organization=SimpleNamespace(pk=other_organization))
        with patch("apps.crm.authentication.get_crm_authenticated_user", return_value=self.user):
            response = dashboard_ai_brain_download(request)
        self.assert_bundle_download(response)
        self.builder.assert_called_once_with(organization=self.organization)

    def test_dashboard_without_crm_session_redirects_before_building(self):
        request = RequestFactory().get(reverse("crm-ai-brain-download"))
        with patch("apps.crm.authentication.get_crm_authenticated_user", return_value=None):
            response = dashboard_ai_brain_download(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("crm-login"))
        self.builder.assert_not_called()

    def test_dashboard_download_rejects_mutations(self):
        request = RequestFactory().post(reverse("crm-ai-brain-download"), {})
        with patch("apps.crm.authentication.get_crm_authenticated_user", return_value=self.user):
            response = dashboard_ai_brain_download(request)
        self.assertEqual(response.status_code, 405)
        self.builder.assert_not_called()

    def test_api_preserves_existing_admin_and_agent_brain_access(self):
        for role in ("admin", "agent"):
            with self.subTest(role=role):
                self.builder.reset_mock()
                response = self.api_request(
                    user=self.author(role), data={"organization_id": str(uuid4())},
                )
                self.assert_bundle_download(response)
                self.builder.assert_called_once_with(organization=self.organization)

    def test_api_rejects_inactive_orgless_superuser_and_api_key_principals(self):
        principals = (
            self.author("admin", is_active=False),
            self.author("admin", organization=SimpleNamespace(pk=uuid4(), is_active=False)),
            self.author("admin", organization=None),
            self.author("superadmin", is_superuser=True),
            SimpleNamespace(is_authenticated=True, organization=self.organization),
        )
        for principal in principals:
            with self.subTest(principal=principal):
                response = self.api_request(user=principal)
                self.assertEqual(response.status_code, 403)
        self.builder.assert_not_called()

    def test_api_requires_authentication_and_is_read_only(self):
        self.assertEqual(self.api_request().status_code, 401)
        self.assertEqual(self.api_request(user=self.user, method="post").status_code, 405)
        self.builder.assert_not_called()

    def test_manifest_revision_cannot_inject_http_headers(self):
        self.bundle["revision"] = "abc\r\nInjected: header/else"
        response = self.api_request(user=self.user)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("\r", response["Content-Disposition"])
        self.assertNotIn("\n", response["Content-Disposition"])
        self.assertNotIn("Injected", response)


class AIBrainBundleDownloadTemplateTests(SimpleTestCase):
    def test_download_uses_saved_configuration_without_replacing_editor_or_sandbox(self):
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
            "form_values": {"organization_name": "Example", "ai_playbook": "##Rules\nBe helpful."},
            "pending_urls": [""], "supported_file_extensions": [".pdf"],
            "knowledge_max_upload_bytes": 1024,
        }))
        self.assertIn(f'href="{reverse("crm-ai-brain-download")}"', html)
        self.assertIn("Download saved AI Brain", html)
        self.assertIn("Save edits before downloading.", html)
        self.assertIn('id="org-ai-settings-form"', html)
        self.assertIn('name="ai_playbook"', html)
        self.assertIn('id="playground-message-input"', html)
