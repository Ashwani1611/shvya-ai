from django.contrib import admin

from apps.integrations.models import WebhookConfiguration, WebhookDelivery


@admin.register(WebhookConfiguration)
class WebhookConfigurationAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "endpoint_url",
        "is_enabled",
        "updated_at",
    )
    list_filter = ("is_enabled",)
    search_fields = ("organization__name", "endpoint_url")
    readonly_fields = ("encrypted_secret", "created_at", "updated_at")


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "organization",
        "event_type",
        "status",
        "attempt_count",
        "response_status",
        "created_at",
    )
    list_filter = ("event_type", "status")
    search_fields = ("organization__name", "lead_id")
    readonly_fields = (
        "webhook",
        "organization",
        "lead_id",
        "event_type",
        "payload",
        "status",
        "attempt_count",
        "response_status",
        "response_body",
        "error_message",
        "delivered_at",
        "created_at",
        "updated_at",
    )
