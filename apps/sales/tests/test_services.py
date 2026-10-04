from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

from PIL import Image

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

    def test_non_finite_numbers_are_rejected(self):
        with self.assertRaises(ValidationError):
            calculate_line_items(
                [{"name": "Service", "qty": "NaN", "rate": "100", "tax_rate": "0"}]
            )

    def test_excessive_document_amount_is_rejected(self):
        with self.assertRaises(ValidationError):
            calculate_line_items(
                [{
                    "name": "Service",
                    "qty": "1000000000",
                    "rate": "999999999999.99",
                    "tax_rate": "0",
                }]
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
    @staticmethod
    def image_bytes(image_format="PNG"):
        content = BytesIO()
        Image.new("RGB", (8, 8), "white").save(content, format=image_format)
        return content.getvalue()

    def test_png_brand_asset_requires_matching_file_signature(self):
        upload = SimpleUploadedFile(
            "logo.png",
            self.image_bytes(),
            content_type="image/png",
        )

        validated = validate_brand_asset(upload, label="Logo")

        self.assertEqual(validated.name, "logo.png")

    def test_mismatched_extensions_are_normalized_for_logo_and_signature(self):
        for label in ("Logo", "Signature"):
            for image_format, name, expected in (
                ("PNG", "asset.jpg", "asset.png"),
                ("JPEG", "asset.png", "asset.jpg"),
                ("JPEG", "asset.jpeg", "asset.jpeg"),
                ("WEBP", "asset.png", "asset.webp"),
            ):
                with self.subTest(label=label, image_format=image_format):
                    content = self.image_bytes(image_format)
                    upload = SimpleUploadedFile(name, content, content_type="image/png")
                    validated = validate_brand_asset(upload, label=label)
                    self.assertEqual(validated.name, expected)
                    self.assertEqual(validated.read(), content)
                    self.assertEqual(validated.content_type, Image.MIME[image_format])

    def test_validation_reads_from_start_and_rewinds_for_storage(self):
        content = self.image_bytes()
        upload = SimpleUploadedFile("logo.png", content, content_type="application/octet-stream")
        upload.read(16)
        self.assertIs(validate_brand_asset(upload, label="Logo"), upload)
        self.assertEqual(upload.tell(), 0)
        self.assertEqual(upload.read(), content)

    def test_corrupt_and_truncated_images_are_rejected(self):
        for content in (b"\x89PNG\r\n\x1a\n" + b"fake", self.image_bytes()[:30]):
            with self.subTest(content=content), self.assertRaises(ValidationError):
                validate_brand_asset(SimpleUploadedFile("logo.png", content), label="Logo")

    def test_empty_oversized_and_unsupported_uploads_are_rejected(self):
        for name, content in (("logo.png", b""), ("logo.svg", b"<svg></svg>"),
                              ("logo.png", b"x" * (5 * 1024 * 1024 + 1))):
            with self.subTest(name=name, size=len(content)), self.assertRaises(ValidationError):
                validate_brand_asset(SimpleUploadedFile(name, content), label="Logo")

    def test_excessive_image_dimensions_are_rejected(self):
        with patch("PIL.Image.MAX_IMAGE_PIXELS", 10), self.assertRaises(ValidationError):
            validate_brand_asset(SimpleUploadedFile("logo.png", self.image_bytes()), label="Logo")

    def test_spoofed_image_extension_is_rejected(self):
        upload = SimpleUploadedFile(
            "signature.jpg",
            b"<html>not an image</html>",
            content_type="image/jpeg",
        )

        with self.assertRaises(ValidationError):
            validate_brand_asset(upload, label="Signature")
