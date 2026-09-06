from types import SimpleNamespace
from unittest.mock import patch

from django.http import HttpResponse
from django.test import SimpleTestCase, TestCase

from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.channels.template_action_ui import _preserve_action, _refresh_pending_templates
from apps.organizations.models import Organization


class TemplateActionPreserverTests(SimpleTestCase):
    def test_editor_response_preserves_clicked_action_after_buttons_are_disabled(self):
        response = HttpResponse(
            '<html><body><form id="template-form">'
            '<button name="action" value="draft">Draft</button>'
            '<button name="action" value="submit">Submit</button>'
            '</form></body></html>'
        )

        fixed = _preserve_action(response)
        html = fixed.content.decode("utf-8")

        self.assertIn("data-shvya-template-action-preserver", html)
        self.assertIn("actionValue.name = 'action'", html)
        self.assertIn("actionValue.value = button.value", html)
        self.assertIn("event.submitter.value", html)


class PendingTemplateRefreshTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Pending Template Org")
        self.account = WhatsAppAccount.objects.create(
            organization=self.org,
            business_name="Main Business",
            phone_number_id="phone-123",
            waba_id="waba-123",
            access_token="test-token",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.user = SimpleNamespace(organization=self.org)

    @patch("apps.channels.template_action_ui.sync_templates")
    def test_pending_template_account_is_synced_from_meta(self, sync_templates):
        WhatsAppTemplate.objects.create(
            organization=self.org,
            account=self.account,
            name="approval_pending",
            body="Hello",
            status=WhatsAppTemplate.Status.PENDING,
            meta_template_id="meta-template-123",
        )

        _refresh_pending_templates(self.user)

        sync_templates.assert_called_once_with(
            organization=self.org,
            account=self.account,
        )

    @patch("apps.channels.template_action_ui.sync_templates")
    def test_no_meta_sync_when_org_has_no_pending_templates(self, sync_templates):
        WhatsAppTemplate.objects.create(
            organization=self.org,
            account=self.account,
            name="already_approved",
            body="Hello",
            status=WhatsAppTemplate.Status.APPROVED,
            meta_template_id="meta-template-456",
        )

        _refresh_pending_templates(self.user)

        sync_templates.assert_not_called()
