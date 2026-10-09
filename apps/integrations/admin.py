from django.contrib import admin

from apps.integrations.models import (
    EmailConfiguration,
    JustDialIntegration,
    JustDialLeadEvent,
    Acres99Integration,
    Acres99Receipt,
    Acres99Event,
    MetaLeadForm,
    MetaLeadPage,
    WebhookConfiguration,
    WebhookDelivery,
)


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


@admin.register(EmailConfiguration)
class EmailConfigurationAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "email_address",
        "provider",
        "is_enabled",
        "last_test_status",
        "last_tested_at",
        "updated_at",
    )
    list_filter = ("provider", "is_enabled", "last_test_status")
    search_fields = (
        "organization__name",
        "email_address",
        "smtp_username",
        "smtp_host",
    )
    readonly_fields = (
        "encrypted_password",
        "last_tested_at",
        "last_error",
        "created_at",
        "updated_at",
    )


@admin.register(MetaLeadPage)
class MetaLeadPageAdmin(admin.ModelAdmin):
    list_display = ("page_name", "page_id", "organization", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("page_name", "page_id", "organization__name")
    readonly_fields = (
        "encrypted_page_access_token",
        "encrypted_app_secret",
        "created_at",
        "updated_at",
    )


@admin.register(MetaLeadForm)
class MetaLeadFormAdmin(admin.ModelAdmin):
    list_display = ("form_name", "form_id", "page", "pipeline", "stage", "is_active")
    list_filter = ("is_active",)
    search_fields = ("form_name", "form_id", "page__page_name")


@admin.register(JustDialIntegration)
class JustDialIntegrationAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "pipeline",
        "stage",
        "is_enabled",
        "last_received_at",
        "received_count",
        "updated_at",
    )
    list_filter = ("is_enabled",)
    search_fields = ("organization__name",)
    readonly_fields = (
        "webhook_token",
        "requested_at",
        "provisioned_at",
        "last_received_at",
        "last_error",
        "received_count",
        "created_count",
        "updated_count",
        "ignored_count",
        "error_count",
        "created_at",
        "updated_at",
    )


@admin.register(JustDialLeadEvent)
class JustDialLeadEventAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "organization",
        "external_lead_id",
        "status",
        "method",
        "lead",
    )
    list_filter = ("status", "method")
    search_fields = (
        "organization__name",
        "external_lead_id",
        "lead__name",
        "lead__phone",
    )
    readonly_fields = (
        "integration",
        "organization",
        "lead",
        "external_lead_id",
        "method",
        "status",
        "payload",
        "error_message",
        "created_at",
    )


@admin.register(Acres99Integration)
class Acres99IntegrationAdmin(admin.ModelAdmin):
    list_display = ("organization", "mode", "pipeline", "stage", "is_enabled", "last_received_at", "last_synced_at")
    list_filter = ("mode", "is_enabled")
    search_fields = ("organization__name",)
    readonly_fields = ("webhook_token", "encrypted_username", "encrypted_password", "sync_cursor",
                       "last_poll_at", "last_received_at", "last_synced_at", "last_error",
                       "received_count", "created_count", "linked_count", "failed_count",
                       "poll_hour_start", "poll_hour_count", "requested_at", "provisioned_at",
                       "created_at", "updated_at")


@admin.register(Acres99Receipt)
class Acres99ReceiptAdmin(admin.ModelAdmin):
    list_display = ("integration", "external_query_id", "direction", "property_id", "received_at")
    search_fields = ("external_query_id", "property_id", "integration__organization__name")
    readonly_fields = ("integration", "external_query_id", "direction", "property_id", "lead", "received_at")


@admin.register(Acres99Event)
class Acres99EventAdmin(admin.ModelAdmin):
    list_display = ("integration", "external_query_id", "direction", "status", "created_at")
    list_filter = ("direction", "status")
    search_fields = ("external_query_id", "integration__organization__name")
    readonly_fields = ("integration", "external_query_id", "direction", "status", "lead", "error_code", "created_at")
