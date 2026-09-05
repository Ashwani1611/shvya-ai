from unittest.mock import Mock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.channels.template_models import WhatsAppTemplateMetadata
from apps.crm.models.attribute import AttributeDefinition
from apps.organizations.models import Organization
from services.channels.template_service import (
    TemplateError,
    available_placeholders,
    build_meta_body,
    copy_template,
    create_template,
    submit_template,
    sync_templates,
)


class WhatsAppTemplateServiceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Template Test Org")
        self.other_org = Organization.objects.create(name="Other Org")
        self.user = User.objects.create_user(
            email="template-admin@example.com",
            organization=self.org,
            password="test-password",
            name="Template Admin",
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

    def test_create_draft_persists_variable_mapping(self):
        template = create_template(
            organization=self.org,
            account=self.account,
            created_by=self.user,
            name="welcome_message",
            body="Hi {{lead_first_name}} from {{org_name}}",
        )
        self.assertEqual(template.status, WhatsAppTemplate.Status.DRAFT)
        self.assertEqual(
            template.meta_state.placeholder_mapping,
            {"1": "lead_first_name", "2": "org_name"},
        )

    def test_custom_crm_attribute_becomes_placeholder_automatically(self):
        AttributeDefinition.objects.create(
            organization=self.org,
            name="Industry",
            key="industry",
            field_type=AttributeDefinition.FieldType.TEXT,
        )
        keys = {
            item["key"]
            for item in available_placeholders(organization=self.org)
        }
        self.assertIn("industry", keys)
        body, mapping = build_meta_body(
            organization=self.org,
            body="For {{industry}} teams",
        )
        self.assertEqual(body, "For {{1}} teams")
        self.assertEqual(mapping, {"1": "industry"})

    def test_unknown_placeholder_is_rejected(self):
        with self.assertRaises(TemplateError):
            build_meta_body(
                organization=self.org,
                body="Hello {{not_a_real_field}}",
            )

    def test_copy_is_a_new_draft_without_remote_identity(self):
        original = create_template(
            organization=self.org,
            account=self.account,
            created_by=self.user,
            name="offer",
            body="Hi {{lead_name}}",
        )
        original.meta_template_id = "meta-1"
        original.status = WhatsAppTemplate.Status.APPROVED
        original.save()
        copied = copy_template(template=original, created_by=self.user)
        self.assertNotEqual(copied.id, original.id)
        self.assertEqual(copied.status, WhatsAppTemplate.Status.DRAFT)
        self.assertEqual(copied.meta_template_id, "")
        self.assertEqual(
            copied.meta_state.placeholder_mapping,
            original.meta_state.placeholder_mapping,
        )

    @patch("services.channels.template_service._remote_templates")
    def test_sync_is_idempotent_and_updates_meta_status(self, remote):
        remote.return_value = [
            {
                "id": "meta-42",
                "name": "remote_welcome",
                "status": "PENDING",
                "category": "MARKETING",
                "language": "en_US",
                "components": [{"type": "BODY", "text": "Hello"}],
            }
        ]
        first = sync_templates(organization=self.org, account=self.account)
        second = sync_templates(organization=self.org, account=self.account)
        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 0)
        self.assertEqual(
            WhatsAppTemplate.objects.filter(
                account=self.account,
                name="remote_welcome",
            ).count(),
            1,
        )
        template = WhatsAppTemplate.objects.get(
            account=self.account,
            name="remote_welcome",
        )
        self.assertEqual(template.status, WhatsAppTemplate.Status.PENDING)
        self.assertEqual(
            template.meta_state.local_status,
            WhatsAppTemplateMetadata.LocalStatus.SYNCED,
        )

    def test_cross_tenant_account_is_rejected(self):
        other = WhatsAppAccount.objects.create(
            organization=self.other_org,
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        with self.assertRaises(TemplateError):
            create_template(
                organization=self.org,
                account=other,
                created_by=self.user,
                name="wrong_tenant",
                body="Hello",
            )

    def test_standard_button_limits_reject_duplicate_website_cta(self):
        with self.assertRaisesRegex(TemplateError, "one Visit Website"):
            create_template(
                organization=self.org,
                account=self.account,
                created_by=self.user,
                name="duplicate_websites",
                body="Hello",
                buttons=[
                    {
                        "type": "visit_website",
                        "text": "Open one",
                        "url": "https://example.com/one",
                    },
                    {
                        "type": "visit_website",
                        "text": "Open two",
                        "url": "https://example.com/two",
                    },
                ],
            )

    @override_settings(META_APP_ID="app-123")
    @patch("services.channels.template_service.WhatsAppClient._post")
    @patch("services.channels.template_service.meta.requests.post")
    def test_media_header_upload_builds_real_meta_header_component(
        self,
        requests_post,
        client_post,
    ):
        session_response = Mock(ok=True)
        session_response.json.return_value = {"id": "upload:abc"}
        upload_response = Mock(ok=True)
        upload_response.json.return_value = {"h": "meta-header-handle"}
        requests_post.side_effect = [session_response, upload_response]
        client_post.return_value = {
            "id": "meta-template-1",
            "status": "PENDING",
        }
        template = create_template(
            organization=self.org,
            account=self.account,
            created_by=self.user,
            name="image_offer",
            body="Hello {{lead_name}}",
            attachment_type=WhatsAppTemplate.AttachmentType.IMAGE,
        )
        media = SimpleUploadedFile(
            "offer.jpg",
            b"jpeg-bytes",
            content_type="image/jpeg",
        )

        submit_template(template=template, attachment_file=media)

        template.refresh_from_db()
        state = template.meta_state
        self.assertEqual(template.status, WhatsAppTemplate.Status.PENDING)
        self.assertEqual(state.header_sample_handle, "meta-header-handle")
        payload = client_post.call_args.args[1]
        self.assertEqual(payload["components"][0]["type"], "HEADER")
        self.assertEqual(payload["components"][0]["format"], "IMAGE")
        self.assertEqual(
            payload["components"][0]["example"]["header_handle"],
            ["meta-header-handle"],
        )
