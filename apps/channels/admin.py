from django.contrib import admin

from .models import WhatsAppAccount, WhatsAppMessage


@admin.register(WhatsAppAccount)
class WhatsAppAccountAdmin(admin.ModelAdmin):

    list_display = [
        "organization",
        "connection_type",
        "status",
        "display_phone_number",
        "is_active",
        "connected_at",
    ]

    list_filter = [
        "connection_type",
        "status",
        "is_active",
    ]

    search_fields = [
        "organization__name",
        "phone_number_id",
        "waba_id",
        "display_phone_number",
    ]

    readonly_fields = [
        "id",
        "connected_at",
        "updated_at",
        "access_token",
    ]

    fields = [
        "id",
        "organization",
        "connection_type",
        "status",
        "phone_number_id",
        "waba_id",
        "display_phone_number",
        "access_token",
        "is_active",
        "connected_at",
        "updated_at",
    ]

    def has_add_permission(self, request):
        # Accounts are created through the org's connect flow
        # (apps.channels.views_flat), not directly in admin --
        # the flow enforces which fields get set for API vs
        # Hosted connections.
        return False


@admin.register(WhatsAppMessage)
class WhatsAppMessageAdmin(admin.ModelAdmin):

    list_display = [
        "organization",
        "direction",
        "status",
        "from_number",
        "to_number",
        "lead",
        "created_at",
    ]

    list_filter = [
        "direction",
        "status",
        "organization",
    ]

    search_fields = [
        "external_id",
        "from_number",
        "to_number",
        "body",
    ]

    readonly_fields = [field.name for field in WhatsAppMessage._meta.fields]

    def has_add_permission(self, request):
        # Messages are only ever created by the webhook/send
        # pipeline, never manually.
        return False

    def has_change_permission(self, request, obj=None):
        # Audit trail -- view only.
        return False