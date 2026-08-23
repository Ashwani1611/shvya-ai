from django.contrib import admin

from .models import (
    APIKey,
    Organization,
    OrganizationPayment,
    OrganizationTag,
)


# ============================================================
# ORGANIZATION PAYMENT INLINE
# ============================================================

class OrganizationPaymentInline(admin.TabularInline):
    model = OrganizationPayment

    extra = 1

    fields = (
        "amount",
        "payment_date",
        "payment_method",
        "reference_number",
        "notes",
    )

    ordering = (
        "-payment_date",
        "-created_at",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )


# ============================================================
# ORGANIZATION ADMIN
# ============================================================

@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):

    list_display = (
        "name",
        "package",
        "payment_mode",
        "total_sale_amount",
        "timezone",
        "is_active",
        "created_at",
    )

    search_fields = (
        "name",
    )

    list_filter = (
        "package",
        "payment_mode",
        "is_active",
    )

    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
    )

    # ---------------------------------------------------------
    # Organization Payments
    # ---------------------------------------------------------

    inlines = (
        OrganizationPaymentInline,
    )


# ============================================================
# ORGANIZATION PAYMENT
# ============================================================

@admin.register(OrganizationPayment)
class OrganizationPaymentAdmin(admin.ModelAdmin):

    list_display = (
        "organization",
        "amount",
        "payment_date",
        "payment_method",
        "reference_number",
        "created_at",
    )

    list_filter = (
        "payment_method",
        "payment_date",
    )

    search_fields = (
        "organization__name",
        "reference_number",
        "notes",
    )

    ordering = (
        "-payment_date",
        "-created_at",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )


# ============================================================
# API KEY ADMIN
# ============================================================

@admin.register(APIKey)
class APIKeyAdmin(admin.ModelAdmin):

    list_display = (
        "name",
        "organization",
        "is_active",
        "last_used_at",
        "expires_at",
    )

    list_filter = (
        "is_active",
    )


# ============================================================
# ORGANIZATION TAG ADMIN
# ============================================================

@admin.register(OrganizationTag)
class OrganizationTagAdmin(admin.ModelAdmin):

    list_display = (
        "name",
    )

    search_fields = (
        "name",
    )