from django.contrib import admin

from apps.core.models import MarketingBookingRequest


@admin.register(MarketingBookingRequest)
class MarketingBookingRequestAdmin(admin.ModelAdmin):
    list_display = ("name", "email", "phone", "company", "preferred_date", "preferred_time", "status", "created_at")
    list_filter = ("status", "preferred_date", "created_at")
    search_fields = ("name", "email", "phone", "company", "goal", "interest")
    readonly_fields = ("id", "source_path", "created_at", "updated_at")
