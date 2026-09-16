from __future__ import annotations

import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from django.conf import settings
from django.core.files.storage import default_storage
from pypdf import PdfReader


MIB = 1024 * 1024

SUPPORTED_FILE_EXTENSIONS = frozenset(
    {
        ".txt",
        ".csv",
        ".xlsx",
        ".pdf",
        ".docx",
    }
)

MAX_UPLOAD_BYTES = int(
    getattr(
        settings,
        "KNOWLEDGE_MAX_UPLOAD_BYTES",
        10 * MIB,
    )
)

MAX_ORGANIZATION_STORAGE_BYTES = int(
    getattr(
        settings,
        "KNOWLEDGE_MAX_ORGANIZATION_STORAGE_BYTES",
        250 * MIB,
    )
)

MAX_ARCHIVE_ENTRIES = int(
    getattr(
        settings,
        "KNOWLEDGE_MAX_ARCHIVE_ENTRIES",
        2_000,
    )
)

MAX_ARCHIVE_UNCOMPRESSED_BYTES = int(
    getattr(
        settings,
        "KNOWLEDGE_MAX_ARCHIVE_UNCOMPRESSED_BYTES",
        50 * MIB,
    )
)

MAX_ARCHIVE_MEMBER_BYTES = int(
    getattr(
        settings,
        "KNOWLEDGE_MAX_ARCHIVE_MEMBER_BYTES",
        25 * MIB,
    )
)

MAX_ARCHIVE_COMPRESSION_RATIO = int(
    getattr(
        settings,
        "KNOWLEDGE_MAX_ARCHIVE_COMPRESSION_RATIO",
        200,
    )
)

MAX_PDF_PAGES = int(
    getattr(
        settings,
        "KNOWLEDGE_MAX_PDF_PAGES",
        1_000,
    )
)

MAX_FILENAME_LENGTH = int(
    getattr(
        settings,
        "KNOWLEDGE_MAX_FILENAME_LENGTH",
        255,
    )
)


class KnowledgeFileSecurityError(ValueError):
    """Raised when an uploaded knowledge file fails security validation."""


@dataclass(frozen=True)
class KnowledgeFileInspection:
    filename: str
    extension: str
    size: int


_REQUIRED_OFFICE_MEMBERS = {
    ".docx": {
        "[Content_Types].xml",
        "word/document.xml",
    },
    ".xlsx": {
        "[Content_Types].xml",
        "xl/workbook.xml",
    },
}

_ALLOWED_ZIP_COMPRESSION = {
    zipfile.ZIP_STORED,
    zipfile.ZIP_DEFLATED,
}


# ============================================================
# PUBLIC VALIDATION
# ============================================================


def validate_knowledge_file(
    file_obj: BinaryIO,
    *,
    filename: str | None = None,
) -> KnowledgeFileInspection:
    """
    Validate an uploaded knowledge file before it reaches a parser.

    The check intentionally does not trust the client-provided MIME type.
    It validates the final extension, real stream size, basic file signature,
    Office ZIP structure, archive expansion metadata, and PDF structure.

    The input stream is restored to its original position where possible.
    """

    if file_obj is None:
        raise KnowledgeFileSecurityError(
            "Uploaded file is required."
        )

    original_position = _safe_tell(file_obj)

    try:
        safe_filename = Path(
            filename
            or getattr(file_obj, "name", "")
            or ""
        ).name

        if not safe_filename:
            raise KnowledgeFileSecurityError(
                "Uploaded file must have a filename."
            )

        if len(safe_filename) > MAX_FILENAME_LENGTH:
            raise KnowledgeFileSecurityError(
                f"Filename is too long. Maximum length is "
                f"{MAX_FILENAME_LENGTH} characters."
            )

        if "\x00" in safe_filename:
            raise KnowledgeFileSecurityError(
                "Filename contains an invalid null byte."
            )

        extension = (
            Path(safe_filename)
            .suffix
            .lower()
            .strip()
        )

        if extension not in SUPPORTED_FILE_EXTENSIONS:
            raise KnowledgeFileSecurityError(
                f"Unsupported file type: {extension or 'unknown'}. "
                f"Supported types: "
                f"{', '.join(sorted(SUPPORTED_FILE_EXTENSIONS))}."
            )

        size = _measure_stream_size(file_obj)

        if size <= 0:
            raise KnowledgeFileSecurityError(
                "Uploaded file is empty."
            )

        if size > MAX_UPLOAD_BYTES:
            raise KnowledgeFileSecurityError(
                f"Uploaded file is too large. Maximum size is "
                f"{MAX_UPLOAD_BYTES // MIB} MiB."
            )

        _seek_start(file_obj)

        if extension in {".txt", ".csv"}:
            _validate_text_file(
                file_obj,
                extension=extension,
            )

        elif extension == ".pdf":
            _validate_pdf_file(file_obj)

        elif extension in {".docx", ".xlsx"}:
            _validate_office_archive(
                file_obj,
                extension=extension,
            )

        return KnowledgeFileInspection(
            filename=safe_filename,
            extension=extension,
            size=size,
        )

    finally:
        _restore_position(
            file_obj,
            original_position,
        )


def validate_organization_knowledge_quota(
    *,
    organization,
    incoming_size: int,
) -> None:
    """
    Enforce a per-organization stored knowledge-file quota.

    Callers should hold a database lock on the Organization row while
    invoking this function so concurrent uploads cannot both pass the quota.
    """

    if organization is None or getattr(organization, "pk", None) is None:
        raise KnowledgeFileSecurityError(
            "Organization is required for knowledge storage validation."
        )

    if incoming_size < 0:
        raise KnowledgeFileSecurityError(
            "Uploaded file size is invalid."
        )

    from apps.ai_engagement.models import Document

    stored_file_names = (
        Document.objects
        .filter(
            organization=organization,
        )
        .exclude(file="")
        .values_list(
            "file",
            flat=True,
        )
        .iterator()
    )

    current_size = 0

    for stored_file_name in stored_file_names:
        if not stored_file_name:
            continue

        try:
            stored_size = int(
                default_storage.size(
                    stored_file_name
                )
            )
        except FileNotFoundError:
            # A stale database reference does not consume storage.
            continue
        except Exception as exc:
            raise KnowledgeFileSecurityError(
                "Unable to verify the organization's knowledge "
                "storage usage."
            ) from exc

        current_size += max(stored_size, 0)

        if (
            current_size + incoming_size
            > MAX_ORGANIZATION_STORAGE_BYTES
        ):
            break

    if (
        current_size + incoming_size
        > MAX_ORGANIZATION_STORAGE_BYTES
    ):
        raise KnowledgeFileSecurityError(
            "Knowledge storage quota exceeded. Maximum stored "
            f"file data per organization is "
            f"{MAX_ORGANIZATION_STORAGE_BYTES // MIB} MiB."
        )


# ============================================================
# STREAM HELPERS
# ============================================================


def _safe_tell(file_obj: BinaryIO) -> int | None:
    try:
        return int(file_obj.tell())
    except Exception:
        return None


def _seek_start(file_obj: BinaryIO) -> None:
    try:
        file_obj.seek(0)
    except Exception as exc:
        raise KnowledgeFileSecurityError(
            "Uploaded file must be seekable for safe validation."
        ) from exc


def _restore_position(
    file_obj: BinaryIO,
    position: int | None,
) -> None:
    try:
        file_obj.seek(
            0 if position is None else position
        )
    except Exception:
        pass


def _measure_stream_size(file_obj: BinaryIO) -> int:
    """Measure the real stream length instead of trusting upload metadata."""

    current_position = _safe_tell(file_obj)

    try:
        file_obj.seek(0, 2)
        size = int(file_obj.tell())
    except Exception as exc:
        raise KnowledgeFileSecurityError(
            "Unable to determine uploaded file size safely."
        ) from exc
    finally:
        _restore_position(
            file_obj,
            current_position,
        )

    return size


# ============================================================
# TEXT / CSV
# ============================================================


def _validate_text_file(
    file_obj: BinaryIO,
    *,
    extension: str,
) -> None:
    _seek_start(file_obj)

    raw = file_obj.read(
        MAX_UPLOAD_BYTES + 1
    )

    if isinstance(raw, str):
        raw_bytes = raw.encode(
            "utf-8",
        )
    else:
        raw_bytes = bytes(raw)

    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise KnowledgeFileSecurityError(
            f"Uploaded file is too large. Maximum size is "
            f"{MAX_UPLOAD_BYTES // MIB} MiB."
        )

    if b"\x00" in raw_bytes:
        raise KnowledgeFileSecurityError(
            f"The {extension} file contains binary null bytes "
            "and is not accepted as text knowledge."
        )

    try:
        raw_bytes.decode(
            "utf-8-sig",
            errors="strict",
        )
    except UnicodeDecodeError as exc:
        raise KnowledgeFileSecurityError(
            f"The {extension} file must contain valid UTF-8 text."
        ) from exc


# ============================================================
# PDF
# ============================================================


def _validate_pdf_file(file_obj: BinaryIO) -> None:
    _seek_start(file_obj)

    header = file_obj.read(8)

    if isinstance(header, str):
        header = header.encode(
            "latin-1",
            errors="ignore",
        )

    if not bytes(header).startswith(b"%PDF-"):
        raise KnowledgeFileSecurityError(
            "File content does not match the .pdf extension."
        )

    _seek_start(file_obj)

    try:
        reader = PdfReader(
            file_obj,
            strict=False,
        )

        if reader.is_encrypted:
            raise KnowledgeFileSecurityError(
                "Encrypted PDF files are not supported."
            )

        page_count = len(
            reader.pages
        )

    except KnowledgeFileSecurityError:
        raise
    except Exception as exc:
        raise KnowledgeFileSecurityError(
            "The uploaded PDF is malformed or cannot be parsed safely."
        ) from exc

    if page_count > MAX_PDF_PAGES:
        raise KnowledgeFileSecurityError(
            f"PDF contains too many pages. Maximum is "
            f"{MAX_PDF_PAGES}."
        )


# ============================================================
# DOCX / XLSX ZIP CONTAINERS
# ============================================================


def _validate_office_archive(
    file_obj: BinaryIO,
    *,
    extension: str,
) -> None:
    _seek_start(file_obj)

    signature = file_obj.read(4)

    if isinstance(signature, str):
        signature = signature.encode(
            "latin-1",
            errors="ignore",
        )

    if bytes(signature) not in {
        b"PK\x03\x04",
        b"PK\x05\x06",
        b"PK\x07\x08",
    }:
        raise KnowledgeFileSecurityError(
            f"File content does not match the {extension} extension."
        )

    _seek_start(file_obj)

    try:
        with zipfile.ZipFile(
            file_obj,
            mode="r",
        ) as archive:
            members = archive.infolist()

            if len(members) > MAX_ARCHIVE_ENTRIES:
                raise KnowledgeFileSecurityError(
                    "Office document contains too many archive entries."
                )

            member_names = {
                member.filename
                for member in members
            }

            required_members = _REQUIRED_OFFICE_MEMBERS[
                extension
            ]

            if not required_members.issubset(
                member_names
            ):
                raise KnowledgeFileSecurityError(
                    f"Archive structure does not match a valid "
                    f"{extension} document."
                )

            total_uncompressed = 0
            total_compressed = 0

            for member in members:
                _validate_archive_member_path(
                    member.filename
                )

                if member.flag_bits & 0x1:
                    raise KnowledgeFileSecurityError(
                        "Encrypted Office archive entries are not supported."
                    )

                if (
                    member.compress_type
                    not in _ALLOWED_ZIP_COMPRESSION
                ):
                    raise KnowledgeFileSecurityError(
                        "Office document uses an unsupported archive "
                        "compression method."
                    )

                unix_mode = (
                    member.external_attr >> 16
                ) & 0o170000

                if unix_mode == stat.S_IFLNK:
                    raise KnowledgeFileSecurityError(
                        "Office document contains a symbolic-link entry."
                    )

                member_size = max(
                    int(member.file_size),
                    0,
                )
                compressed_size = max(
                    int(member.compress_size),
                    0,
                )

                if member_size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise KnowledgeFileSecurityError(
                        "Office document contains an archive member "
                        "that is too large after decompression."
                    )

                total_uncompressed += member_size
                total_compressed += compressed_size

                if (
                    total_uncompressed
                    > MAX_ARCHIVE_UNCOMPRESSED_BYTES
                ):
                    raise KnowledgeFileSecurityError(
                        "Office document expands beyond the safe "
                        "decompression limit."
                    )

                if member_size >= MIB:
                    ratio = member_size / max(
                        compressed_size,
                        1,
                    )

                    if ratio > MAX_ARCHIVE_COMPRESSION_RATIO:
                        raise KnowledgeFileSecurityError(
                            "Office document contains a suspiciously "
                            "compressed archive member."
                        )

            if total_uncompressed:
                total_ratio = (
                    total_uncompressed
                    / max(total_compressed, 1)
                )

                if (
                    total_ratio
                    > MAX_ARCHIVE_COMPRESSION_RATIO
                ):
                    raise KnowledgeFileSecurityError(
                        "Office document has a suspicious overall "
                        "compression ratio."
                    )

    except KnowledgeFileSecurityError:
        raise
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as exc:
        raise KnowledgeFileSecurityError(
            f"The uploaded {extension} file is not a valid Office archive."
        ) from exc
    except Exception as exc:
        raise KnowledgeFileSecurityError(
            f"The uploaded {extension} file cannot be parsed safely."
        ) from exc


def _validate_archive_member_path(
    member_name: str,
) -> None:
    normalized = (
        member_name
        .replace("\\", "/")
    )

    path = PurePosixPath(
        normalized
    )

    if (
        normalized.startswith("/")
        or normalized.startswith("\\")
        or ".." in path.parts
        or (path.parts and ":" in path.parts[0])
    ):
        raise KnowledgeFileSecurityError(
            "Office document contains an unsafe archive path."
        )
