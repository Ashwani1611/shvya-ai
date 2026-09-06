from unittest.mock import patch

from django.test import TestCase

from apps.accounts.models import User
from apps.channels import template_action_ui, template_ui
from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.organizations.models import Organization
from services.channels import template_service
from services.channels.template_meta_fix import (
    submit_template,
    sync_templates,
)


class WhatsAppTemplateMetaFixTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Template Meta Fix Org")
        self.user = User.objects.create_user(
            email="template-meta-fix@example.com",
            organization=self.org,
            password="test-password",
            name="Template Meta Admin",
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

    @patch("services.channels.template_meta_fix.base.WhatsAppClient._post")
    def test_submit_adds_meta_body_examples_for_crm_placeholders(self, client_post):
        client_post.return_value = {
            "id": "meta-template-variable-1",
            "status": "PENDING",
        }
        template = template_service.create_template(
            organization=self.org,
            account=self.account,
            created_by=self.user,
            name="welcome_with_variables",
            body="Hi {{lead_first_name}} from {{org_name}}",
            category=WhatsAppTemplate.Category.UTILITY,
        )

        submit_template(template=template)

        payload = client_post.call_args.args[1]
        body_component = next(
            component
            for component in payload["components"]
            if component["type"] == "BODY"
        )
        self.assertEqual(body_component["text"], "Hi {{1}} from {{2}}")
        self.assertEqual(
            body_component["example"]["body_text"],
            [["John", "Template Meta Fix Org"]],
        )
        template.refresh_from_db()
        self.assertEqual(template.status, WhatsAppTemplate.Status.PENDING)
        self.assertEqual(
            template.meta_state.components,
            payload["components"],
        )

    @patch("services.channels.template_meta_fix.base._remote_templates")
    def test_sync_removes_meta_none_rejection_sentinel(self, remote_templates):
        remote_templates.return_value = [
            {
                "id": "meta-approved-1",
                "name": "hello_world",
                "status": "APPROVED",
                "category": "UTILITY",
                "language": "en_US",
                "components": [{"type": "BODY", "text": "Hello world"}],
                "rejected_reason": "NONE",
            }
        ]

        sync_templates(organization=self.org, account=self.account)

        template = WhatsAppTemplate.objects.get(
            account=self.account,
            meta_template_id="meta-approved-1",
        )
        self.assertEqual(template.status, WhatsAppTemplate.Status.APPROVED)
        self.assertEqual(template.rejection_reason, "")

    @patch("services.channels.template_meta_fix.base._remote_templates")
    def test_sync_preserves_real_meta_rejection_reason(self, remote_templates):
        remote_templates.return_value = [
            {
                "id": "meta-rejected-1",
                "name": "bad_template",
                "status": "REJECTED",
                "category": "UTILITY",
                "language": "en_US",
                "components": [{"type": "BODY", "text": "Hello"}],
                "rejected_reason": "INVALID_FORMAT",
            }
        ]

        sync_templates(organization=self.org, account=self.account)

        template = WhatsAppTemplate.objects.get(
            account=self.account,
            meta_template_id="meta-rejected-1",
        )
        self.assertEqual(template.status, WhatsAppTemplate.Status.REJECTED)
        self.assertEqual(template.rejection_reason, "INVALID_FORMAT")

    def test_active_template_ui_uses_meta_compatibility_service(self):
        self.assertIs(template_ui.submit_template, submit_template)
        self.assertIs(template_ui.sync_templates, sync_templates)
        self.assertTrue(callable(template_action_ui.template_create))
