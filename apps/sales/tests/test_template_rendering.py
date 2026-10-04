import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase

from apps.accounts.models import User
from apps.crm.models import Lead
from apps.organizations.models import Organization
from apps.sales.document_services import (
    SalesDeliveryError,
    default_email_body,
    default_email_subject,
    default_whatsapp_body,
    delivery_drafts,
    merge_values,
    render_document_html,
    render_text_template,
    snapshot_document_presentation,
)
from apps.sales.delivery_services import (
    deliver_email,
    deliver_whatsapp,
    _sales_template_message,
)
from apps.sales.email_design import email_preview
from apps.sales.models import SalesDocument, SalesTemplate
from apps.sales.preview import template_preview


class TemplateRenderingTests(SimpleTestCase):
    def setUp(self):
        self.org = Organization(name="Example & Co")
        self.user = User(organization=self.org, role=User.Role.ADMIN, name="Agent")
        self.lead = Lead(
            organization=self.org,
            name="Alex <Morgan>",
            email="alex@example.com",
            phone="+919876543210",
            attributes={"Budget": 0, "document_number": "evil"},
        )
        self.template = SalesTemplate(
            organization=self.org,
            document_type="quotation",
            body_template="<h1>{{document.title}}</h1><p>{{recipient.name}}</p>{{document.content}}",
            header_text="{{organization.name}}",
            footer_text="For {{recipient.name}}",
            email_subject_template="{{document.number}}",
            email_body_template="Hi {{recipient.name}}",
            whatsapp_body_template="{{document_number}}: {{document_total}}",
        )
        self.doc = SalesDocument(
            organization=self.org,
            lead=self.lead,
            template=self.template,
            document_type="quotation",
            document_number="QT-123",
            title="Proposal",
            currency="INR",
            total="11800",
            subtotal="10000",
            tax_total="1800",
            discount_total="0",
            issue_date=date(2026, 9, 24),
            recipient_name=self.lead.name,
            content="Prepared for {{recipient.name}}",
            terms="Thank you",
        )
        snapshot_document_presentation(self.doc)

    def test_aliases_and_zero_custom_attribute(self):
        values = merge_values(self.doc)
        self.assertEqual(
            render_text_template(
                "{{ document_number }} / {{lead.attribute.Budget}}", values
            ),
            "QT-123 / 0",
        )

    def test_unknown_variables_block_send(self):
        for send, kwargs in (
            (deliver_email, {"subject": "Hi"}),
            (deliver_whatsapp, {}),
        ):
            with (
                self.subTest(send=send.__name__),
                self.assertRaisesMessage(SalesDeliveryError, "unknown"),
            ):
                send(document=self.doc, user=self.user, body="Hi {{unknown}}", **kwargs)

    def test_default_messages_resolve_document_number_for_every_type(self):
        for document_type in ("quotation", "agreement", "invoice"):
            with self.subTest(document_type=document_type):
                self.doc.document_type = document_type
                self.doc.template = None
                snapshot_document_presentation(self.doc)
                drafts = delivery_drafts(self.doc, public_url="https://example.com/doc")
                for key, default in (
                    ("email_subject", default_email_subject),
                    ("email_body", default_email_body),
                    ("whatsapp_body", default_whatsapp_body),
                ):
                    self.assertIn("{{document.number}}", default(document_type))
                    self.assertIn("QT-123", drafts[key])
                    self.assertNotIn("{document.number}", drafts[key])

    def test_saved_single_brace_templates_resolve_without_resnapshot(self):
        self.doc.email_subject_snapshot = "Quotation {document.number}"
        self.doc.email_body_snapshot = "Hi {{recipient.name}}, your quotation {document.number} is ready."
        self.doc.whatsapp_body_snapshot = self.doc.email_body_snapshot
        drafts = delivery_drafts(self.doc, public_url="https://example.com/doc")
        self.assertEqual(drafts["email_subject"], "Quotation QT-123")
        for key in ("email_body", "whatsapp_body"):
            self.assertEqual(drafts[key], "Hi Alex <Morgan>, your quotation QT-123 is ready.")

    def test_single_brace_fields_and_literal_blocks(self):
        values = merge_values(self.doc)
        source = '{ document.number } / {{document_number}} / {"count": 1} / { color: red; }'
        self.assertEqual(
            render_text_template(source, values, strict=True),
            'QT-123 / QT-123 / {"count": 1} / { color: red; }',
        )
        with self.assertRaisesMessage(SalesDeliveryError, "document.missing"):
            render_text_template("{document.missing}", values, strict=True)

    def test_user_edited_drafts_resolved_at_delivery_boundary(self):
        for send, kwargs in (
            (deliver_email, {"subject": "{document.number} {{document.url}}"}),
            (deliver_whatsapp, {}),
        ):
            with (
                self.subTest(send=send.__name__),
                patch(
                    "apps.sales.delivery_services._delivery_row",
                    side_effect=RuntimeError("stop before IO"),
                ) as row,
            ):
                with self.assertRaisesMessage(RuntimeError, "stop before IO"):
                    send(
                        document=self.doc,
                        user=self.user,
                        body="Hello {{recipient.name}}, document {document.number}",
                        base_url="https://example.com",
                        **kwargs,
                    )
                self.assertEqual(row.call_args.kwargs["body"], "Hello Alex <Morgan>, document QT-123")
                if kwargs:
                    self.assertIn(
                        "QT-123 https://example.com/", row.call_args.kwargs["subject"]
                    )

    def test_layout_escapes_values_and_resolves_nested_content(self):
        rendered = render_document_html(self.doc)
        self.assertIn("Alex &lt;Morgan&gt;", rendered)
        self.assertIn("Prepared for Alex &lt;Morgan&gt;", rendered)
        self.assertNotIn("{{", rendered)

    def test_branding_and_drafts_resolve(self):
        self.assertEqual(self.doc.presentation_snapshot["header_text"], "Example & Co")
        self.assertEqual(
            delivery_drafts(self.doc, public_url="https://example.com")[
                "whatsapp_body"
            ],
            "QT-123: INR 11,800.00",
        )

    def test_email_design_escapes_text_and_fits_brand_assets(self):
        self.doc.presentation_snapshot["logo_url"] = "https://example.com/logo.png"
        self.doc.presentation_snapshot["signature_url"] = "javascript:alert(1)"
        rendered = email_preview(self.doc, "Hello <script>alert(1)</script>")
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("javascript:", rendered)
        self.assertIn("max-height:70px", rendered)
        self.assertIn("QT-123", rendered)

    def test_meta_values_preserve_zero_whitespace_and_reserved_fields(self):
        state = SimpleNamespace(
            placeholder_mapping={"1": "document_number", "2": "lead.attribute.Budget"},
            language="en_US",
        )
        template = SimpleNamespace(
            body="{{ 1 }} / {{lead.attribute.Budget}}", id="x", name="sales"
        )
        with patch("services.channels.template_service.state_for", return_value=state):
            text, payload = _sales_template_message(
                template=template,
                document=self.doc,
                user=self.user,
                pdf_url="https://example.com/doc.pdf",
                public_url="https://example.com/doc",
            )
        self.assertEqual(text, "QT-123 / 0")
        self.assertEqual(payload["components"][1]["parameters"][1]["text"], "0")

    def test_unmapped_meta_variable_blocked(self):
        with patch(
            "services.channels.template_service.state_for",
            return_value=SimpleNamespace(placeholder_mapping={}, language="en_US"),
        ):
            with self.assertRaisesMessage(SalesDeliveryError, "Map every"):
                _sales_template_message(
                    template=SimpleNamespace(body="{{1}}", id="x", name="sales"),
                    document=self.doc,
                    user=self.user,
                    pdf_url="https://example.com/a.pdf",
                    public_url="",
                )

    def test_preview_uses_current_content_and_never_saves(self):
        request = RequestFactory().post(
            "/",
            {
                "document_type": "invoice",
                "body_template": "<h2>Hello {{recipient.name}}</h2><script>alert(1)</script>{{items_table}}",
                "item_show_name": "on",
                "email_body_template": "For {{recipient.name}}",
                "whatsapp_body_template": "{{document_total}}",
            },
        )
        request.crm_user = self.user
        response = template_preview.__wrapped__(request)
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertIn("Hello Alex Morgan", payload["html"])
        self.assertIn("Professional services", payload["html"])
        self.assertNotIn("<script>", payload["html"])
        self.assertEqual(payload["whatsapp_body"], "INR 11,800.00")
        self.assertIn("For Alex Morgan", payload["email_html"])

    def test_preview_rejects_non_admin(self):
        request = RequestFactory().post("/", {})
        request.crm_user = SimpleNamespace(organization_id=self.org.id, role="agent")
        self.assertEqual(template_preview.__wrapped__(request).status_code, 403)
