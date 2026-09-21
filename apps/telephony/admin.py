from django.contrib import admin

from .models import (
    CallAppRelease,
    CallDevice,
    CallEvent,
    CallIntelligenceResult,
    CallIntelligenceSettings,
    CallRecord,
)


@admin.register(CallIntelligenceSettings)
class CallIntelligenceSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "organization", "enabled", "auto_create_answered_incoming",
        "auto_create_missed", "updated_at",
    )
    search_fields = ("organization__name",)


@admin.register(CallDevice)
class CallDeviceAdmin(admin.ModelAdmin):
    list_display = (
        "name", "organization", "user", "model", "app_version",
        "is_active", "last_seen_at",
    )
    list_filter = ("is_active", "organization")
    search_fields = ("name", "device_id", "user__email", "organization__name")


@admin.register(CallRecord)
class CallRecordAdmin(admin.ModelAdmin):
    list_display = (
        "phone_number", "organization", "user", "direction", "status",
        "lead", "talk_duration_seconds", "ended_at",
    )
    list_filter = ("direction", "status", "source", "organization")
    search_fields = (
        "phone_number", "raw_phone_number", "contact_name",
        "lead__name", "user__email",
    )


@admin.register(CallEvent)
class CallEventAdmin(admin.ModelAdmin):
    list_display = ("event_uuid", "call", "event_type", "occurred_at")
    list_filter = ("event_type",)


@admin.register(CallIntelligenceResult)
class CallIntelligenceResultAdmin(admin.ModelAdmin):
    list_display = ("call", "intent", "sentiment", "outcome", "analyzed_at")


@admin.register(CallAppRelease)
class CallAppReleaseAdmin(admin.ModelAdmin):
    list_display = (
        "version_name", "version_code", "is_active", "is_mandatory",
        "min_android_sdk", "created_at",
    )
    list_filter = ("is_active", "is_mandatory")
