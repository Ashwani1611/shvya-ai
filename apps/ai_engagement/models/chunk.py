from django.db import models
from pgvector.django import VectorField

from apps.organizations.models import Organization

from .document import Document


class Chunk(models.Model):
    """
    A searchable piece of text extracted from an organization
    knowledge document.

    The embedding vector is stored directly in PostgreSQL using
    pgvector. The embedding is nullable so knowledge sources can
    exist before an external embedding provider is configured.
    """

    EMBEDDING_DIMENSIONS = 1536

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="chunks",
    )

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="ai_knowledge_chunks",
    )

    content = models.TextField()

    chunk_index = models.PositiveIntegerField(
        default=0,
    )

    embedding = VectorField(
        dimensions=EMBEDDING_DIMENSIONS,
        null=True,
        blank=True,
    )

    is_active = models.BooleanField(
        default=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = [
            "document",
            "chunk_index",
        ]

        constraints = [
            models.UniqueConstraint(
                fields=[
                    "document",
                    "chunk_index",
                ],
                name="uniq_document_chunk_index",
            ),
        ]

    def __str__(self):
        return (
            f"{self.document.name} "
            f"→ Chunk {self.chunk_index}"
        )