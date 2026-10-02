import io
import subprocess
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from apps.ai_engagement.services.knowledge import KnowledgeExtractionError, KnowledgeIngestionService
from apps.ai_engagement.services.knowledge_ocr import KnowledgeOCRError, extract_pdf_pages


def scanned_pdf():
    image = Image.new("RGB", (700, 120), "white")
    ImageDraw.Draw(image).text((15, 35), "SHVYA support includes onboarding", fill="black")
    stream = io.BytesIO()
    canvas = Canvas(stream)
    canvas.drawImage(ImageReader(image), 30, 500, width=500, height=90)
    canvas.save()
    stream.seek(0)
    return stream


class KnowledgeOCRTests(SimpleTestCase):
    def test_scanned_page_uses_ocr_and_retains_page_reference(self):
        with patch("apps.ai_engagement.services.knowledge_ocr.extract_pdf_pages", return_value={1: "Support includes onboarding."}) as ocr:
            result = KnowledgeIngestionService().extract_file_text(scanned_pdf(), filename="scan.pdf")
        self.assertIn("Page 1", result)
        self.assertIn("Support includes onboarding", result)
        self.assertEqual(ocr.call_args.args[1], [1])

    def test_text_pdf_does_not_call_ocr(self):
        stream = io.BytesIO()
        canvas = Canvas(stream)
        canvas.drawString(30, 500, "Support includes onboarding.")
        canvas.save()
        stream.seek(0)
        with patch("apps.ai_engagement.services.knowledge_ocr.extract_pdf_pages") as ocr:
            self.assertIn("onboarding", KnowledgeIngestionService().extract_file_text(stream, filename="text.pdf"))
        ocr.assert_not_called()

    def test_failed_ocr_does_not_silently_publish_incomplete_knowledge(self):
        with patch("apps.ai_engagement.services.knowledge_ocr.extract_pdf_pages", side_effect=KnowledgeOCRError("OCR unavailable")):
            with self.assertRaisesRegex(KnowledgeExtractionError, "OCR unavailable"):
                KnowledgeIngestionService().extract_file_text(scanned_pdf(), filename="scan.pdf")

    @override_settings(KNOWLEDGE_OCR_MAX_PAGES=1)
    def test_page_budget_prevents_unbounded_work(self):
        with patch("apps.ai_engagement.services.knowledge_ocr.subprocess.run") as run:
            with self.assertRaisesRegex(KnowledgeOCRError, "Split"):
                extract_pdf_pages(io.BytesIO(b"pdf"), [1, 2])
        run.assert_not_called()

    def test_temporary_files_are_cleaned_and_input_position_restored(self):
        paths = []
        def run(command, **kwargs):
            self.assertLessEqual(kwargs["timeout"], 45)
            self.assertTrue(kwargs["check"])
            if command[0] == "pdftoppm":
                prefix = Path(command[-1])
                paths.append(prefix.parent)
                prefix.with_suffix(".png").write_bytes(b"image")
            else:
                Path(command[2]).with_suffix(".txt").write_text("Recognized text", encoding="utf-8")
        stream = io.BytesIO(b"pdf")
        stream.seek(1)
        with patch("apps.ai_engagement.services.knowledge_ocr.subprocess.run", side_effect=run):
            self.assertEqual(extract_pdf_pages(stream, [1]), {1: "Recognized text"})
        self.assertEqual(stream.tell(), 1)
        self.assertFalse(paths[0].exists())

    def test_timeout_is_actionable(self):
        with patch("apps.ai_engagement.services.knowledge_ocr.subprocess.run", side_effect=subprocess.TimeoutExpired("ocr", 1)):
            with self.assertRaisesRegex(KnowledgeOCRError, "scan quality"):
                extract_pdf_pages(io.BytesIO(b"pdf"), [1])
