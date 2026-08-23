from django.contrib import admin
from django.contrib.admin.models import LogEntry


@admin.register(LogEntry)
class LogEntryAdmin(admin.ModelAdmin):
    """
    Read-only administrative activity log for SHVYA Admin.

    LogEntry is maintained by Django automatically whenever
    an authenticated admin user performs an administrative
    action.
    """

    list_display = (
        "action_time",
        "user",
        "content_type",
        "object_repr",
        "action_flag",
    )

    list_filter = (
        "action_flag",
        "content_type",
        "action_time",
    )

    search_fields = (
        "object_repr",
        "change_message",
        "user__username",
        "user__email",
    )

    ordering = (
        "-action_time",
    )

    readonly_fields = (
        "action_time",
        "user",
        "content_type",
        "object_id",
        "object_repr",
        "action_flag",
        "change_message",
    )

    def has_add_permission(self, request):
        """
        Admin activity is generated automatically.
        Administrators should not manually create log entries.
        """
        return False

    def has_change_permission(self, request, obj=None):
        """
        Activity records must remain immutable.
        """
        return False

    def has_delete_permission(self, request, obj=None):
        """
        Preserve the administrative audit trail.
        """
        return False