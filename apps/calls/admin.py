from django.contrib import admin

from .models import (
    CallDevice,
    CallEvent,
    CallIntelligence,
    CallIntelligenceSettings,
    CallRecord,
)


@admin.register(CallIntelligenceSettings)
class CallIntelligenceSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "is_enabled",
        "auto_create_answered",
        "auto_create_missed",
        "auto_create_outbound",
        "updated_at",
    )
    search_fields = ("organization__name",)


@admin.register(CallDevice)
class CallDeviceAdmin(admin.ModelAdmin):
    list_display = (
        "device_name",
        "organization",
        "user",
        "model",
        "app_version",
        "is_active",
        "last_seen_at",
    )
    list_filter = ("is_active", "manufacturer", "android_version")
    search_fields = (
        "device_name",
        "device_uuid",
        "organization__name",
        "user__email",
    )


@admin.register(CallRecord)
class CallRecordAdmin(admin.ModelAdmin):
    list_display = (
        "called_at",
        "organization",
        "user",
        "direction",
        "status",
        "phone_number",
        "lead",
        "duration_seconds",
    )
    list_filter = ("source", "direction", "status", "lead_match_status")
    search_fields = (
        "phone_number",
        "raw_phone_number",
        "contact_name",
        "lead__name",
        "user__email",
    )
    readonly_fields = ("first_received_at", "last_received_at", "created_at", "updated_at")


@admin.register(CallEvent)
class CallEventAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "call", "event_type", "device", "received_at")
    list_filter = ("event_type",)
    search_fields = ("event_uuid", "call__phone_number")


@admin.register(CallIntelligence)
class CallIntelligenceAdmin(admin.ModelAdmin):
    list_display = ("call", "analysis_status", "intent", "ai_score", "analyzed_at")
    list_filter = ("analysis_status", "intent", "sentiment")
    search_fields = ("call__phone_number", "call__lead__name", "summary")
