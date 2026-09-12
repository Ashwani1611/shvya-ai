from django.contrib import admin

from apps.integrations.models import (
    EmailConfiguration,
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
