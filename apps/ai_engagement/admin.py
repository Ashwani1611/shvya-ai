from django.contrib import admin

from .models import Chunk, Document, KnowledgeSource


@admin.register(KnowledgeSource)
class KnowledgeSourceAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "source_type",
        "name",
        "url",
        "is_active",
        "updated_at",
    )
    list_filter = ("source_type", "is_active")
    search_fields = ("name", "url", "organization__name")


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "name",
        "source_key",
        "version",
        "processing_status",
        "is_active",
        "updated_at",
    )
    list_filter = ("processing_status", "is_active")
    search_fields = ("name", "source_key", "organization__name")
    readonly_fields = (
        "processing_status",
        "processing_error",
        "created_at",
        "updated_at",
    )


@admin.register(Chunk)
class ChunkAdmin(admin.ModelAdmin):
    list_display = (
        "document",
        "chunk_index",
        "organization",
        "is_active",
        "updated_at",
    )
    list_filter = ("is_active",)
    search_fields = ("content",)
