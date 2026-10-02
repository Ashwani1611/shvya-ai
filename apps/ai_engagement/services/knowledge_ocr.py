"""Bounded, local OCR for image-only PDF pages; never a chat-model fallback."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
from time import monotonic

from django.conf import settings


class KnowledgeOCRError(ValueError):
    pass


def extract_pdf_pages(file_obj, page_numbers):
    """Return page-number/text pairs, or fail without publishing partial content."""
    pages = list(page_numbers)
    if not pages:
        return {}
    max_pages = min(max(int(getattr(settings, "KNOWLEDGE_OCR_MAX_PAGES", 30)), 1), 100)
    if len(pages) > max_pages:
        raise KnowledgeOCRError(f"This PDF needs OCR on {len(pages)} pages. Split it into files of up to {max_pages} scanned pages.")
    deadline = monotonic() + min(max(int(getattr(settings, "KNOWLEDGE_OCR_TIMEOUT", 180)), 10), 300)
    languages = str(getattr(settings, "KNOWLEDGE_OCR_LANGUAGES", "eng+hin"))
    position = file_obj.tell()
    try:
        with tempfile.TemporaryDirectory(prefix="shvya-ocr-") as folder:
            source = Path(folder) / "source.pdf"
            file_obj.seek(0)
            raw = file_obj.read(10 * 1024 * 1024 + 1)
            if len(raw) > 10 * 1024 * 1024:
                raise KnowledgeOCRError("PDF exceeds the OCR input size limit.")
            source.write_bytes(raw)
            result = {}
            for number in pages:
                prefix = Path(folder) / "page"
                commands = [
                    ["pdftoppm", "-f", str(number), "-l", str(number), "-singlefile", "-scale-to", "2400", "-png", str(source), str(prefix)],
                    ["tesseract", str(prefix) + ".png", str(prefix), "-l", languages, "--psm", "3"],
                ]
                for command in commands:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise KnowledgeOCRError("OCR took too long. Split the scanned PDF into smaller files and retry.")
                    try:
                        subprocess.run(command, check=True, timeout=min(remaining, 45),
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       env={**os.environ, "OMP_THREAD_LIMIT": "1"})
                    except FileNotFoundError as exc:
                        raise KnowledgeOCRError("OCR tools are unavailable on the ingestion worker. Ask an administrator to update the application image, then retry.") from exc
                    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
                        raise KnowledgeOCRError("OCR could not read this PDF. Check scan quality and retry with a smaller file.") from exc
                output = prefix.with_suffix(".txt")
                if output.stat().st_size > 1024 * 1024:
                    raise KnowledgeOCRError("OCR output exceeds the safe page size limit.")
                result[number] = output.read_text(encoding="utf-8").strip()
                output.unlink()
                prefix.with_suffix(".png").unlink()
            return result
    finally:
        file_obj.seek(position)
