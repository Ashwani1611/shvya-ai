from __future__ import annotations

import logging

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.ai_engagement.models import Document, KnowledgeSource
from apps.ai_engagement.serializers.document import (
    DocumentSerializer,
    DocumentUploadSerializer,
    KnowledgeSourceCreateSerializer,
    KnowledgeSourceSerializer,
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

    Ingestion (text extraction, chunking, embedding) runs
    asynchronously via Celery — the response only confirms the
    Document was accepted.
    """

    permission_classes = [IsAuthenticated, IsOrgMember]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    pagination_class = StandardResultsPagination

    def get(self, request, *args, **kwargs):

        queryset = (
            Document.objects
            .filter(
                organization=request.user.organization,
            )
            .order_by(
                "-updated_at",
            )
        )

        is_active = request.query_params.get("is_active")

        if is_active is not None:
            queryset = queryset.filter(
                is_active=is_active.lower() == "true",
            )

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request)
        serializer = DocumentSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request, *args, **kwargs):

        serializer = DocumentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        uploaded_file = data["file"]
        name = data.get("name") or uploaded_file.name
        source_key = data.get("source_key") or name

        document = Document.objects.create(
            organization=request.user.organization,
            name=name,
            source_key=source_key,
            file=uploaded_file,
            processing_status=Document.ProcessingStatus.PENDING,
            is_active=False,
        )

        ingest_and_index_document.delay(
            document_id=document.id,
            organization_id=request.user.organization.id,
        )

        return Response(
            DocumentSerializer(document).data,
            status=status.HTTP_202_ACCEPTED,
        )


class DocumentDetailAPIView(APIView):
    """
    Retrieve or deactivate a single knowledge document.

    Deactivating a document removes it from retrieval (it stops
    matching document__is_active=True in
    KnowledgeRetrievalService) without deleting its chunk history.
    """

    permission_classes = [IsAuthenticated, IsOrgMember]

    def _get_document(self, request, document_id):
        return get_object_or_404(
            Document,
            id=document_id,
            organization=request.user.organization,
        )

    def get(self, request, document_id, *args, **kwargs):
        document = self._get_document(request, document_id)
        return Response(DocumentSerializer(document).data)

    def delete(self, request, document_id, *args, **kwargs):
        document = self._get_document(request, document_id)
        document.is_active = False
        document.save(update_fields=["is_active", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class DocumentReindexAPIView(APIView):
    """
    Re-queue embedding generation for a document's chunks —
    e.g. after an embedding provider failure, or an
    OPENAI_EMBEDDING_MODEL change.

    Does not re-parse the source file; only regenerates vectors.
    """

    permission_classes = [IsAuthenticated, IsOrgMember]

    def post(self, request, document_id, *args, **kwargs):

        document = get_object_or_404(
            Document,
            id=document_id,
            organization=request.user.organization,
        )

        if document.processing_status != Document.ProcessingStatus.COMPLETED:
            return Response(
                {
                    "message": (
                        "Only a document with processing_status "
                        "'completed' can be re-indexed."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        reindex_document_embeddings.delay(
            document_id=document.id,
            organization_id=request.user.organization.id,
        )

        return Response(status=status.HTTP_202_ACCEPTED)


# ============================================================
# KNOWLEDGE SOURCES (URLS)
# ============================================================


class KnowledgeSourceListAPIView(APIView):
    """
    List an organization's URL knowledge sources, or register a
    new URL to be fetched, chunked, and embedded.
    """

    permission_classes = [IsAuthenticated, IsOrgMember]
    pagination_class = StandardResultsPagination

    def get(self, request, *args, **kwargs):

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
        page = paginator.paginate_queryset(queryset, request)
        serializer = KnowledgeSourceSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request, *args, **kwargs):

        serializer = KnowledgeSourceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        source = KnowledgeSource.objects.create(
            organization=request.user.organization,
            source_type=KnowledgeSource.SourceType.URL,
            name=data.get("name", ""),
            url=data["url"],
        )

        ingest_and_index_url_source.delay(
            source_id=source.id,
            organization_id=request.user.organization.id,
        )

        return Response(
            KnowledgeSourceSerializer(source).data,
            status=status.HTTP_202_ACCEPTED,
        )


class KnowledgeSourceDetailAPIView(APIView):
    """
    Retrieve or deactivate a single URL knowledge source record.

    Deactivating the source only stops it being listed/reused as
    a bookkeeping entry — it does not by itself deactivate the
    Document(s) already published from it. Deactivate the
    Document via DocumentDetailAPIView to remove it from
    retrieval.
    """

    permission_classes = [IsAuthenticated, IsOrgMember]

    def _get_source(self, request, source_id):
        return get_object_or_404(
            KnowledgeSource,
            id=source_id,
            organization=request.user.organization,
        )

    def get(self, request, source_id, *args, **kwargs):
        source = self._get_source(request, source_id)
        return Response(KnowledgeSourceSerializer(source).data)

    def delete(self, request, source_id, *args, **kwargs):
        source = self._get_source(request, source_id)
        source.is_active = False
        source.save(update_fields=["is_active", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)
