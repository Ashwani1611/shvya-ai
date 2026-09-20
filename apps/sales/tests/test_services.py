from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from apps.sales.services import (
    calculate_line_items,
    render_text_template,
    sanitize_layout_html,
    validate_brand_asset,
)


class SalesCalculationTests(SimpleTestCase):
    def test_totals_are_calculated_server_side(self):
        totals = calculate_line_items(
            [
                {
                    "name": "Implementation",
                    "qty": "2",
                    "rate": "100",
                    "tax_rate": "18",
                }
            ],
            discount_total="10",
        )

        self.assertEqual(totals["subtotal"], Decimal("200.00"))
        self.assertEqual(totals["tax_total"], Decimal("36.00"))
        self.assertEqual(totals["discount_total"], Decimal("10.00"))
        self.assertEqual(totals["total"], Decimal("226.00"))
        self.assertEqual(totals["items"][0]["amount"], "200.00")

    def test_negative_financial_values_are_rejected(self):
        with self.assertRaises(ValidationError):
            calculate_line_items(
                [{"name": "Unsafe", "qty": "1", "rate": "-5", "tax_rate": "0"}]
            )

    def test_discount_cannot_exceed_document_amount(self):
        with self.assertRaises(ValidationError):
            calculate_line_items(
                [{"name": "Service", "qty": "1", "rate": "100", "tax_rate": "0"}],
                discount_total="101",
            )


class SalesTemplateSafetyTests(SimpleTestCase):
    def test_layout_sanitizer_removes_scripts_events_and_unsafe_styles(self):
        cleaned = sanitize_layout_html(
            '<section onclick="steal()" style="color: #111; position: fixed; '
            'background-image: url(javascript:bad)">'
            '<script>alert(1)</script><h1 style="text-align:center">Safe</h1>'
            "</section>"
        )

        self.assertNotIn("<script", cleaned)
        self.assertNotIn("onclick", cleaned)
        self.assertNotIn("position:", cleaned)
        self.assertNotIn("javascript:", cleaned)
        self.assertIn("color: #111", cleaned)
        self.assertIn("text-align: center", cleaned)
        self.assertIn("Safe", cleaned)

    def test_merge_renderer_personalizes_known_fields_and_preserves_unknown_fields(self):
        rendered = render_text_template(
            "Hi {{ recipient.name }} — {{document.number}} — {{custom.future}}",
            {
                "recipient.name": "Aarav",
                "document.number": "QT-00001",
            },
        )

        self.assertEqual(
            rendered,
            "Hi Aarav — QT-00001 — {{custom.future}}",
        )


class SalesBrandAssetTests(SimpleTestCase):
    def test_png_brand_asset_requires_matching_file_signature(self):
        upload = SimpleUploadedFile(
            "logo.png",
            b"\x89PNG\r\n\x1a\n" + b"safe-image-body",
            content_type="image/png",
        )

        validated = validate_brand_asset(upload, label="Logo")

        self.assertEqual(validated.name, "logo.png")

    def test_spoofed_image_extension_is_rejected(self):
        upload = SimpleUploadedFile(
            "signature.jpg",
            b"<html>not an image</html>",
            content_type="image/jpeg",
        )

        with self.assertRaises(ValidationError):
            validate_brand_asset(upload, label="Signature")
