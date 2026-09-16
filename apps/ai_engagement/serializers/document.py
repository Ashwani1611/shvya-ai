from __future__ import annotations

from rest_framework import serializers

from apps.ai_engagement.models import Document, KnowledgeSource
from apps.ai_engagement.services.knowledge_file_security import (
    KnowledgeFileSecurityError,
    validate_knowledge_file,
)


# ============================================================
# DOCUMENTS (FILE UPLOADS)
# ============================================================


class DocumentSerializer(serializers.ModelSerializer):
    """
    Read-only representation of a knowledge Document.
    """

    chunk_count = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            "id",
            "name",
            "source_key",
            "version",
            "file",
            "source_url",
            "processing_status",
            "processing_error",
            "is_active",
            "chunk_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_chunk_count(self, obj):
        return obj.chunks.filter(is_active=True).count()


class DocumentUploadSerializer(serializers.Serializer):
    """
    Validate a new knowledge file before a Document is created.

    Validation is content-aware and does not trust the client-provided
    Content-Type header. Creating the Document record and queueing ingestion
    still happens in the view.
    """

    file = serializers.FileField()

    name = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
    )

    source_key = serializers.CharField(
        max_length=500,
        required=False,
        allow_blank=True,
    )

    def validate_file(self, value):
        try:
            validate_knowledge_file(
                value,
                filename=value.name,
            )
        except KnowledgeFileSecurityError as exc:
            raise serializers.ValidationError(
                str(exc)
            ) from exc

        return value


# ============================================================
# KNOWLEDGE SOURCES (URLS)
# ============================================================


class KnowledgeSourceSerializer(serializers.ModelSerializer):
    """
    Read-only representation of a URL knowledge source.
    """

    class Meta:
        model = KnowledgeSource
        fields = [
            "id",
            "source_type",
            "name",
            "url",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class KnowledgeSourceCreateSerializer(serializers.Serializer):
    """
    Validates a new URL knowledge source before ingestion.
    """

    url = serializers.URLField()

    name = serializers.CharField(
        max_length=255,
        required=False,
        allow_blank=True,
    )
