from django.contrib import admin

from apps.triggers.models import SmartTrigger, TriggerExecution


@admin.register(SmartTrigger)
class SmartTriggerAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "organization",
        "event_type",
        "is_active",
        "successful_runs",
        "failed_runs",
        "last_fired_at",
        "updated_at",
    )
    list_filter = ("event_type", "is_active", "condition_mode")
    search_fields = ("name", "description", "organization__name")
    readonly_fields = (
        "successful_runs",
        "failed_runs",
        "last_fired_at",
        "created_at",
        "updated_at",
    )
    list_select_related = ("organization", "created_by")


@admin.register(TriggerExecution)
class TriggerExecutionAdmin(admin.ModelAdmin):
    list_display = (
        "trigger",
        "organization",
        "lead",
        "event_type",
        "status",
        "matched",
        "created_at",
    )
    list_filter = ("status", "event_type", "matched")
    search_fields = (
        "trigger__name",
        "organization__name",
        "lead__name",
        "lead__phone",
        "event_id",
    )
    list_select_related = ("trigger", "organization", "lead")
    readonly_fields = (
        "organization",
        "trigger",
        "lead",
        "event_id",
        "event_type",
        "event_payload",
        "status",
        "matched",
        "skip_reason",
        "action_results",
        "error",
        "started_at",
        "finished_at",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
