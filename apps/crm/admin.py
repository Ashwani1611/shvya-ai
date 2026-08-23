from django.contrib import admin

from .models import (
    Pipeline,
    Stage,
    Lead,
    LeadContact,
    PipelinePermission,
    LeadNote,
    LeadCall,
    LeadReminder,
    Tag,
    LeadTag,
)


# ============================================================
# PIPELINE / STAGE
# ============================================================


class StageInline(admin.TabularInline):
    model = Stage
    extra = 0
    ordering = ("display_order",)


@admin.register(Pipeline)
class PipelineAdmin(admin.ModelAdmin):
    """
    SHVYA Admin pipeline management.
    """

    list_display = (
        "name",
        "organization",
        "is_active",
        "created_at",
    )

    list_filter = (
        "organization",
        "is_active",
    )

    search_fields = (
        "name",
        "organization__name",
    )

    date_hierarchy = "created_at"

    ordering = (
        "-created_at",
    )

    list_per_page = 25

    list_select_related = (
        "organization",
    )

    inlines = [
        StageInline,
    ]


@admin.register(Stage)
class StageAdmin(admin.ModelAdmin):
    """
    SHVYA Admin stage management.
    """

    list_display = (
        "name",
        "pipeline",
        "display_order",
        "is_active",
    )

    list_filter = (
        "pipeline",
        "pipeline__organization",
        "is_active",
    )

    search_fields = (
        "name",
        "pipeline__name",
        "pipeline__organization__name",
    )

    ordering = (
        "pipeline",
        "display_order",
    )

    list_per_page = 25

    list_select_related = (
        "pipeline",
        "pipeline__organization",
    )


# ============================================================
# LEAD CONTACT
# ============================================================


class LeadContactInline(admin.TabularInline):
    model = LeadContact
    extra = 0


@admin.register(LeadContact)
class LeadContactAdmin(admin.ModelAdmin):
    """
    SHVYA Admin lead-contact management.
    """

    list_display = (
        "lead",
        "channel",
        "handle",
        "verified",
    )

    list_filter = (
        "channel",
        "verified",
    )

    search_fields = (
        "handle",
        "lead__name",
        "lead__email",
        "lead__phone",
    )

    ordering = (
        "lead",
        "channel",
    )

    list_per_page = 25

    list_select_related = (
        "lead",
    )


# ============================================================
# LEAD NOTES
# ============================================================


class LeadNoteInline(admin.TabularInline):
    model = LeadNote
    extra = 0

    fields = (
        "note",
        "note_type",
        "created_by",
        "created_at",
    )

    readonly_fields = (
        "created_by",
        "created_at",
    )

    ordering = (
        "-created_at",
    )


@admin.register(LeadNote)
class LeadNoteAdmin(admin.ModelAdmin):
    """
    SHVYA Admin lead-note management.
    """

    list_display = (
        "lead",
        "note_type",
        "created_by",
        "created_at",
    )

    list_filter = (
        "note_type",
        "lead__organization",
    )

    search_fields = (
        "lead__name",
        "lead__email",
        "lead__phone",
        "note",
        "created_by__email",
        "created_by__name",
    )

    date_hierarchy = "created_at"

    ordering = (
        "-created_at",
    )

    list_per_page = 25

    list_select_related = (
        "lead",
        "lead__organization",
        "created_by",
    )


# ============================================================
# LEAD
# ============================================================


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    """
    SHVYA Admin lead management.

    Provides operational search and filtering across
    organization, pipeline, stage, contact information,
    and creation date.
    """

    list_display = (
        "name",
        "phone",
        "organization",
        "pipeline",
        "stage",
        "created_at",
    )

    list_filter = (
        "organization",
        "pipeline",
        "stage",
    )

    search_fields = (
        "name",
        "phone",
        "email",
        "organization__name",
        "pipeline__name",
        "stage__name",
    )

    date_hierarchy = "created_at"

    ordering = (
        "-created_at",
    )

    list_per_page = 25

    list_select_related = (
        "organization",
        "pipeline",
        "stage",
    )

    inlines = [
        LeadContactInline,
        LeadNoteInline,
    ]

    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
    )


# ============================================================
# PIPELINE PERMISSION
# ============================================================


@admin.register(PipelinePermission)
class PipelinePermissionAdmin(admin.ModelAdmin):
    """
    SHVYA Admin pipeline access-control management.
    """

    list_display = (
        "user",
        "pipeline",
        "can_view_pipeline",
        "can_create_leads",
        "can_edit_leads",
        "can_manage_pipeline",
    )

    list_filter = (
        "pipeline",
        "pipeline__organization",
        "can_view_pipeline",
        "can_create_leads",
        "can_edit_leads",
        "can_manage_pipeline",
    )

    search_fields = (
        "user__email",
        "user__name",
        "pipeline__name",
        "pipeline__organization__name",
    )

    ordering = (
        "pipeline",
        "user",
    )

    list_per_page = 25

    list_select_related = (
        "user",
        "pipeline",
        "pipeline__organization",
    )


# ============================================================
# LEAD CALL
# ============================================================


@admin.register(LeadCall)
class LeadCallAdmin(admin.ModelAdmin):
    """
    SHVYA Admin call activity management.
    """

    list_display = (
        "lead",
        "status",
        "duration_seconds",
        "user",
        "called_at",
    )

    list_filter = (
        "status",
        "lead__organization",
    )

    search_fields = (
        "lead__name",
        "lead__email",
        "lead__phone",
        "user__email",
        "user__name",
    )

    date_hierarchy = "called_at"

    ordering = (
        "-called_at",
    )

    list_per_page = 25

    list_select_related = (
        "lead",
        "lead__organization",
        "user",
    )


# ============================================================
# LEAD REMINDER
# ============================================================


@admin.register(LeadReminder)
class LeadReminderAdmin(admin.ModelAdmin):
    """
    SHVYA Admin follow-up reminder management.
    """

    list_display = (
        "title",
        "lead",
        "assigned_to",
        "due_at",
        "status",
    )

    list_filter = (
        "status",
        "lead__organization",
    )

    search_fields = (
        "title",
        "lead__name",
        "lead__email",
        "lead__phone",
        "assigned_to__email",
        "assigned_to__name",
    )

    date_hierarchy = "due_at"

    ordering = (
        "due_at",
    )

    list_per_page = 25

    list_select_related = (
        "lead",
        "lead__organization",
        "assigned_to",
    )


# ============================================================
# TAG
# ============================================================


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    """
    SHVYA Admin CRM tag management.
    """

    list_display = (
        "name",
        "organization",
        "color",
    )

    list_filter = (
        "organization",
    )

    search_fields = (
        "name",
        "organization__name",
    )

    ordering = (
        "organization",
        "name",
    )

    list_per_page = 25

    list_select_related = (
        "organization",
    )


# ============================================================
# LEAD TAG
# ============================================================


@admin.register(LeadTag)
class LeadTagAdmin(admin.ModelAdmin):
    """
    SHVYA Admin lead-tag relationship management.
    """

    list_display = (
        "lead",
        "tag",
    )

    list_filter = (
        "tag__organization",
        "tag",
    )

    search_fields = (
        "lead__name",
        "lead__email",
        "lead__phone",
        "tag__name",
    )

    ordering = (
        "lead",
        "tag",
    )

    list_per_page = 25

    list_select_related = (
        "lead",
        "tag",
    )