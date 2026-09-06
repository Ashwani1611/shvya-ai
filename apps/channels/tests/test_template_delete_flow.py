from unittest.mock import Mock, patch

from django.test import TestCase

from apps.accounts.models import User
from apps.channels import template_action_ui, template_ui
from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.channels.template_models import WhatsAppTemplateMetadata, WhatsAppTemplateOperation
from apps.organizations.models import Organization
from services.channels.template_delete_fix import TemplateError, delete_template


class WhatsAppTemplateImmediateDeleteTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Template Delete Org")
        self.user = User.objects.create_user(
            email="template-delete@example.com",
            organization=self.org,
            password="test-password",
            name="Template Delete Admin",
            role=User.Role.ADMIN,
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.org,
            business_name="Main Business",
            phone_number_id="123456789",
            waba_id="987654321",
            access_token="test-token",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )

    def _remote_template(self, name="delete_me"):
        template = WhatsAppTemplate.objects.create(
            organization=self.org,
            account=self.account,
            created_by=self.user,
            name=name,
            body="Hello",
            category=WhatsAppTemplate.Category.UTILITY,
            status=WhatsAppTemplate.Status.APPROVED,
            meta_template_id=f"meta-{name}",
        )
        WhatsAppTemplateMetadata.objects.create(
            template=template,
            local_status=WhatsAppTemplateMetadata.LocalStatus.SYNCED,
        )
        return template

    @patch("services.channels.template_delete_fix.base.meta.requests.delete")
    def test_successful_meta_delete_removes_template_immediately(self, requests_delete):
        response = Mock(ok=True, status_code=200, text='{"success": true}')
        response.json.return_value = {"success": True}
        requests_delete.return_value = response
        template = self._remote_template()
        template_id = template.id

        delete_template(template=template)

        self.assertFalse(WhatsAppTemplate.objects.filter(id=template_id).exists())
        self.assertFalse(WhatsAppTemplateMetadata.objects.filter(template_id=template_id).exists())
        audit = WhatsAppTemplateOperation.objects.get(
            organization=self.org,
            operation=WhatsAppTemplateOperation.Operation.DELETE,
        )
        self.assertTrue(audit.success)
        self.assertIsNone(audit.template_id)

    @patch("services.channels.template_delete_fix.base.meta.requests.delete")
    def test_meta_404_is_treated_as_already_deleted(self, requests_delete):
        response = Mock(ok=False, status_code=404, text='{"error":{"message":"Not found"}}')
        response.json.return_value = {"error": {"message": "Not found"}}
        requests_delete.return_value = response
        template = self._remote_template(name="already_gone")
        template_id = template.id

        delete_template(template=template)

        self.assertFalse(WhatsAppTemplate.objects.filter(id=template_id).exists())

    @patch("services.channels.template_delete_fix.base.meta.requests.delete")
    def test_meta_delete_failure_keeps_local_template(self, requests_delete):
        response = Mock(
            ok=False,
            status_code=400,
            text='{"error":{"message":"Cannot delete template","code":100}}',
        )
        requests_delete.return_value = response
        template = self._remote_template(name="keep_on_failure")
        template_id = template.id

        with self.assertRaisesRegex(TemplateError, "Cannot delete template"):
            delete_template(template=template)

        self.assertTrue(WhatsAppTemplate.objects.filter(id=template_id).exists())
        template.refresh_from_db()
        self.assertEqual(template.status, WhatsAppTemplate.Status.APPROVED)
        audit = WhatsAppTemplateOperation.objects.get(
            organization=self.org,
            operation=WhatsAppTemplateOperation.Operation.DELETE,
        )
        self.assertFalse(audit.success)
        self.assertEqual(audit.template_id, template_id)

    def test_active_template_ui_uses_immediate_delete_service(self):
        self.assertIs(template_ui.delete_template, delete_template)
        self.assertTrue(callable(template_action_ui.template_list))
