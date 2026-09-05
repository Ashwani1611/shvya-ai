from __future__ import annotations

from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import models, transaction

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
        - permanently delete sources/documents
        - legacy activate/deactivate compatibility

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
        its next Document version.

        The Document starts as PENDING and inactive. Celery
        ingestion is responsible for processing and publishing
        the completed version.
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

            latest_document = (
                Document.objects
                .select_for_update()
                .filter(
                    organization=organization,
                    source_key=filename,
                )
                .order_by(
                    "-version",
                )
                .first()
            )

            next_version = (
                latest_document.version + 1
                if latest_document is not None
                else 1
            )

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
                version=next_version,
                file=uploaded_file,
                source_url="",
                processing_status=(
                    Document.ProcessingStatus.PENDING
                ),
                processing_error="",
                is_active=False,
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
    # DELETE DOCUMENT
    # ========================================================

    def delete_document(
        self,
        *,
        document: Document,
    ) -> None:
        """
        Permanently delete an organization-owned Document.

        The Document -> Chunk relationship already uses CASCADE,
        so deleting the Document removes its Chunk rows and
        therefore their stored embeddings.

        The physical uploaded file is deleted from storage too.
        """

        if document is None:
            raise KnowledgeSourceServiceError(
                "Document is required."
            )

        if document.organization_id is None:
            raise KnowledgeSourceServiceError(
                "Document must belong to an organization."
            )

        stored_file_name = (
            document.file.name
            if document.file
            else ""
        )

        with transaction.atomic():
            document.delete()

        if stored_file_name:
            default_storage.delete(
                stored_file_name
            )

    # ========================================================
    # DELETE SOURCE
    # ========================================================

    def delete_source(
        self,
        *,
        source: KnowledgeSource,
    ) -> None:
        """
        Permanently delete a KnowledgeSource and every Document
        version belonging to that logical source.

        Uploaded files are deleted from storage.
        Document deletion cascades to Chunk/embedding rows.
        """

        self._validate_source(
            source=source,
        )

        documents = (
            Document.objects
            .filter(
                organization_id=source.organization_id,
            )
        )

        if source.source_type == (
            KnowledgeSource.SourceType.URL
        ):
            try:
                normalized_url = (
                    self.ingestion_service._normalize_url(
                        source.url,
                    )
                )
            except KnowledgeExtractionError as exc:
                raise KnowledgeSourceServiceError(
                    str(exc)
                ) from exc

            documents = documents.filter(
                source_key=normalized_url,
            )

        else:
            documents = documents.filter(
                models.Q(source_key=source.name)
                | models.Q(name=source.name),
            )

        documents = list(
            documents.order_by("id")
        )

        stored_file_names = [
            document.file.name
            for document in documents
            if document.file
        ]

        with transaction.atomic():

            for document in documents:
                document.delete()

            source.delete()

        for stored_file_name in stored_file_names:
            default_storage.delete(
                stored_file_name
            )

    # ========================================================
    # DEACTIVATE SOURCE
    # ========================================================

    def deactivate_source(
        self,
        *,
        source: KnowledgeSource,
    ) -> KnowledgeSource:
        """
        Legacy compatibility method.

        The current UI should not expose source deactivation.
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
        Legacy compatibility method.

        New sources are already created active.
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