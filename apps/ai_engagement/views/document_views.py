from __future__ import annotations

import logging

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.parsers import (
    FormParser,
    JSONParser,
    MultiPartParser,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ai_engagement.models import (
    Document,
    KnowledgeSource,
)
from apps.ai_engagement.serializers.document import (
    DocumentSerializer,
    DocumentUploadSerializer,
    KnowledgeSourceCreateSerializer,
    KnowledgeSourceSerializer,
)
from apps.ai_engagement.services.knowledge_source import (
    KnowledgeSourceService,
    KnowledgeSourceServiceError,
)
from apps.ai_engagement.tasks import (
    ingest_and_index_document,
    ingest_and_index_url_source,
    reindex_document_embeddings,
)
from apps.core.pagination import StandardResultsPagination
from apps.core.permissions import IsOrgMember


logger = logging.getLogger(__name__)


# ============================================================
# DOCUMENTS (FILE UPLOADS)
# ============================================================


class DocumentListAPIView(APIView):
    """
    List an organization's knowledge documents, or upload a new
    file as the next version of a knowledge source.

    Ingestion runs asynchronously through Celery.
    """

    permission_classes = [
        IsAuthenticated,
        IsOrgMember,
    ]

    parser_classes = [
        MultiPartParser,
        FormParser,
        JSONParser,
    ]

    pagination_class = StandardResultsPagination

    def get(
        self,
        request,
        *args,
        **kwargs,
    ):
        queryset = (
            Document.objects
            .filter(
                organization=request.user.organization,
            )
            .order_by(
                "-updated_at",
            )
        )

        is_active = request.query_params.get(
            "is_active"
        )

        if is_active is not None:
            queryset = queryset.filter(
                is_active=(
                    is_active.lower() == "true"
                ),
            )

        paginator = self.pagination_class()

        page = paginator.paginate_queryset(
            queryset,
            request,
        )

        serializer = DocumentSerializer(
            page,
            many=True,
        )

        return (
            paginator.get_paginated_response(
                serializer.data
            )
        )

    def post(
        self,
        request,
        *args,
        **kwargs,
    ):
        serializer = DocumentUploadSerializer(
            data=request.data
        )

        serializer.is_valid(
            raise_exception=True
        )

        data = serializer.validated_data

        uploaded_file = data["file"]

        name = (
            data.get("name")
            or uploaded_file.name
        )

        source_key = (
            data.get("source_key")
            or uploaded_file.name
        )

        with transaction.atomic():

            latest_document = (
                Document.objects
                .select_for_update()
                .filter(
                    organization=request.user.organization,
                    source_key=source_key,
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

            document = Document.objects.create(
                organization=request.user.organization,
                name=name,
                source_key=source_key,
                version=next_version,
                file=uploaded_file,
                processing_status=(
                    Document.ProcessingStatus.PENDING
                ),
                processing_error="",
                is_active=False,
            )

            transaction.on_commit(
                lambda document_id=document.id,
                organization_id=(
                    request.user.organization.id
                ): ingest_and_index_document.delay(
                    document_id=document_id,
                    organization_id=organization_id,
                )
            )

        return Response(
            DocumentSerializer(document).data,
            status=status.HTTP_202_ACCEPTED,
        )


class DocumentDetailAPIView(APIView):
    """
    Retrieve or permanently delete a single knowledge document.
    """

    permission_classes = [
        IsAuthenticated,
        IsOrgMember,
    ]

    def _get_document(
        self,
        request,
        document_id,
    ):
        return get_object_or_404(
            Document,
            id=document_id,
            organization=request.user.organization,
        )

    def get(
        self,
        request,
        document_id,
        *args,
        **kwargs,
    ):
        document = self._get_document(
            request,
            document_id,
        )

        return Response(
            DocumentSerializer(document).data
        )

    def delete(
        self,
        request,
        document_id,
        *args,
        **kwargs,
    ):
        document = self._get_document(
            request,
            document_id,
        )

        try:
            KnowledgeSourceService().delete_document(
                document=document,
            )

        except KnowledgeSourceServiceError as exc:
            return Response(
                {
                    "detail": str(exc),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            status=status.HTTP_204_NO_CONTENT
        )


class DocumentReindexAPIView(APIView):
    """
    Re-queue embedding generation for a completed document.
    """

    permission_classes = [
        IsAuthenticated,
        IsOrgMember,
    ]

    def post(
        self,
        request,
        document_id,
        *args,
        **kwargs,
    ):
        document = get_object_or_404(
            Document,
            id=document_id,
            organization=request.user.organization,
        )

        if document.processing_status != (
            Document.ProcessingStatus.COMPLETED
        ):
            return Response(
                {
                    "message": (
                        "Only a document with "
                        "processing_status 'completed' "
                        "can be re-indexed."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        reindex_document_embeddings.delay(
            document_id=document.id,
            organization_id=request.user.organization.id,
        )

        return Response(
            status=status.HTTP_202_ACCEPTED
        )


# ============================================================
# KNOWLEDGE SOURCES (URLS)
# ============================================================


class KnowledgeSourceListAPIView(APIView):
    """
    List an organization's URL knowledge sources, or register
    a new URL for asynchronous ingestion.
    """

    permission_classes = [
        IsAuthenticated,
        IsOrgMember,
    ]

    pagination_class = StandardResultsPagination

    def get(
        self,
        request,
        *args,
        **kwargs,
    ):
        queryset = (
            KnowledgeSource.objects
            .filter(
                organization=request.user.organization,
            )
            .order_by(
                "-updated_at",
            )
        )

        paginator = self.pagination_class()

        page = paginator.paginate_queryset(
            queryset,
            request,
        )

        serializer = KnowledgeSourceSerializer(
            page,
            many=True,
        )

        return (
            paginator.get_paginated_response(
                serializer.data
            )
        )

    def post(
        self,
        request,
        *args,
        **kwargs,
    ):
        serializer = KnowledgeSourceCreateSerializer(
            data=request.data
        )

        serializer.is_valid(
            raise_exception=True
        )

        data = serializer.validated_data

        source = KnowledgeSource.objects.create(
            organization=request.user.organization,
            source_type=(
                KnowledgeSource.SourceType.URL
            ),
            name=data.get(
                "name",
                "",
            ),
            url=data["url"],
            is_active=True,
        )

        transaction.on_commit(
            lambda source_id=source.id,
            organization_id=(
                request.user.organization.id
            ): ingest_and_index_url_source.delay(
                source_id=source_id,
                organization_id=organization_id,
            )
        )

        return Response(
            KnowledgeSourceSerializer(
                source
            ).data,
            status=status.HTTP_202_ACCEPTED,
        )


class KnowledgeSourceDetailAPIView(APIView):
    """
    Retrieve or permanently delete a single URL knowledge source.

    Deletion removes the source, every matching document version,
    its chunks/embeddings, and stored files.
    """

    permission_classes = [
        IsAuthenticated,
        IsOrgMember,
    ]

    def _get_source(
        self,
        request,
        source_id,
    ):
        return get_object_or_404(
            KnowledgeSource,
            id=source_id,
            organization=request.user.organization,
        )

    def get(
        self,
        request,
        source_id,
        *args,
        **kwargs,
    ):
        source = self._get_source(
            request,
            source_id,
        )

        return Response(
            KnowledgeSourceSerializer(
                source
            ).data
        )

    def delete(
        self,
        request,
        source_id,
        *args,
        **kwargs,
    ):
        source = self._get_source(
            request,
            source_id,
        )

        try:
            KnowledgeSourceService().delete_source(
                source=source,
            )

        except KnowledgeSourceServiceError as exc:
            return Response(
                {
                    "detail": str(exc),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            status=status.HTTP_204_NO_CONTENT
        )