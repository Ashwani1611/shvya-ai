from __future__ import annotations

from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.ai_engagement.models import (
    Document,
    KnowledgeSource,
)
from apps.ai_engagement.services.knowledge import (
    KnowledgeExtractionError,
    KnowledgeIngestionService,
)


class KnowledgeSourceServiceError(Exception):
    """
    Raised when a KnowledgeSource operation cannot be completed
    safely.
    """


class KnowledgeSourceService:
    """
    Organization-scoped management service for KnowledgeSource.

    Responsibilities:

        - create URL sources
        - create file sources
        - process URL sources
        - process file Documents
        - activate/deactivate sources

    This service does NOT implement:

        - text extraction
        - cleaning
        - chunking
        - embedding generation
        - vector retrieval

    Those responsibilities remain in the existing specialized
    services.
    """

    def __init__(
        self,
        *,
        ingestion_service: KnowledgeIngestionService | None = None,
    ) -> None:
        self.ingestion_service = (
            ingestion_service
            or KnowledgeIngestionService()
        )

    # ========================================================
    # CREATE URL SOURCE
    # ========================================================

    def create_url_source(
        self,
        *,
        organization,
        url: str,
        name: str = "",
    ) -> KnowledgeSource:
        """
        Create an organization-owned URL KnowledgeSource.
        """

        if organization is None:
            raise KnowledgeSourceServiceError(
                "Organization is required."
            )

        try:
            normalized_url = (
                self.ingestion_service._normalize_url(
                    url,
                )
            )
        except KnowledgeExtractionError as exc:
            raise KnowledgeSourceServiceError(
                str(exc)
            ) from exc

        source_name = (
            name or normalized_url
        ).strip()

        if not source_name:
            source_name = normalized_url

        source = KnowledgeSource(
            organization=organization,
            source_type=KnowledgeSource.SourceType.URL,
            name=source_name,
            url=normalized_url,
            is_active=True,
        )

        try:
            source.full_clean()
            source.save()

        except ValidationError as exc:
            raise KnowledgeSourceServiceError(
                str(exc)
            ) from exc

        return source

    # ========================================================
    # CREATE FILE SOURCE
    # ========================================================

    def create_file_source(
        self,
        *,
        organization,
        uploaded_file,
        name: str = "",
    ) -> tuple[KnowledgeSource, Document]:
        """
        Create an organization-owned FILE KnowledgeSource and
        its initial Document.

        File processing itself is deliberately separate and is
        performed through process_file_source().
        """

        if organization is None:
            raise KnowledgeSourceServiceError(
                "Organization is required."
            )

        if uploaded_file is None:
            raise KnowledgeSourceServiceError(
                "Uploaded file is required."
            )

        filename = Path(
            uploaded_file.name or ""
        ).name

        if not filename:
            raise KnowledgeSourceServiceError(
                "Uploaded file must have a filename."
            )

        extension = (
            Path(filename)
            .suffix
            .lower()
            .strip()
        )

        if (
            extension
            not in self.ingestion_service.SUPPORTED_FILE_EXTENSIONS
        ):
            raise KnowledgeSourceServiceError(
                f"Unsupported file type: {extension}"
            )

        source_name = (
            name or filename
        ).strip()

        if not source_name:
            source_name = filename

        with transaction.atomic():

            source = KnowledgeSource.objects.create(
                organization=organization,
                source_type=KnowledgeSource.SourceType.FILE,
                name=source_name,
                url="",
                is_active=True,
            )

            document = Document.objects.create(
                organization=organization,
                name=source_name,
                source_key=filename,
                version=1,
                file=uploaded_file,
                source_url="",
                processing_status=(
                    Document.ProcessingStatus.PENDING
                ),
                processing_error="",
                is_active=True,
            )

        return source, document

    # ========================================================
    # PROCESS URL SOURCE
    # ========================================================

    def process_url_source(
        self,
        *,
        source: KnowledgeSource,
    ) -> Document:
        """
        Process a URL KnowledgeSource using the existing
        KnowledgeIngestionService.

        The ingestion service is responsible for:

            - fetching the URL
            - extracting text
            - cleaning
            - chunking
            - versioning
            - publishing the new Document version
        """

        self._validate_source(
            source=source,
        )

        if source.source_type != (
            KnowledgeSource.SourceType.URL
        ):
            raise KnowledgeSourceServiceError(
                "KnowledgeSource must be a URL source."
            )

        if not source.is_active:
            raise KnowledgeSourceServiceError(
                "Cannot process an inactive KnowledgeSource."
            )

        try:
            self.ingestion_service.ingest_url(
                source,
            )

            normalized_url = (
                self.ingestion_service._normalize_url(
                    source.url,
                )
            )

        except KnowledgeExtractionError as exc:
            raise KnowledgeSourceServiceError(
                str(exc)
            ) from exc

        document = (
            Document.objects
            .filter(
                organization=source.organization,
                source_key=normalized_url,
                is_active=True,
                processing_status=(
                    Document.ProcessingStatus.COMPLETED
                ),
            )
            .order_by(
                "-version",
            )
            .first()
        )

        if document is None:
            raise KnowledgeSourceServiceError(
                "URL processing completed but no active "
                "completed Document version was found."
            )

        return document

    # ========================================================
    # PROCESS FILE DOCUMENT
    # ========================================================

    def process_file_source(
        self,
        *,
        document: Document,
    ) -> int:
        """
        Process an existing uploaded Document using the existing
        KnowledgeIngestionService.

        Returns:
            Number of chunks created.
        """

        if document is None:
            raise KnowledgeSourceServiceError(
                "Document is required."
            )

        if document.organization_id is None:
            raise KnowledgeSourceServiceError(
                "Document must belong to an organization."
            )

        try:
            return self.ingestion_service.ingest_document(
                document,
            )

        except KnowledgeExtractionError as exc:
            raise KnowledgeSourceServiceError(
                str(exc)
            ) from exc

    # ========================================================
    # DEACTIVATE SOURCE
    # ========================================================

    def deactivate_source(
        self,
        *,
        source: KnowledgeSource,
    ) -> KnowledgeSource:
        """
        Deactivate a KnowledgeSource.

        Existing Documents are retained. The source itself will
        no longer be considered active by source-management
        operations.
        """

        self._validate_source(
            source=source,
        )

        if not source.is_active:
            return source

        source.is_active = False

        source.save(
            update_fields=[
                "is_active",
                "updated_at",
            ]
        )

        return source

    # ========================================================
    # ACTIVATE SOURCE
    # ========================================================

    def activate_source(
        self,
        *,
        source: KnowledgeSource,
    ) -> KnowledgeSource:
        """
        Reactivate a KnowledgeSource.
        """

        self._validate_source(
            source=source,
        )

        if source.is_active:
            return source

        source.is_active = True

        source.save(
            update_fields=[
                "is_active",
                "updated_at",
            ]
        )

        return source

    # ========================================================
    # VALIDATION
    # ========================================================

    @staticmethod
    def _validate_source(
        *,
        source: KnowledgeSource,
    ) -> None:
        """
        Validate that the source exists and belongs to an
        organization.
        """

        if source is None:
            raise KnowledgeSourceServiceError(
                "KnowledgeSource is required."
            )

        if source.organization_id is None:
            raise KnowledgeSourceServiceError(
                "KnowledgeSource must belong to an organization."
            )