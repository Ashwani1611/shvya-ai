from django.contrib import admin

from apps.analytics.models import AnalyticsSettings


@admin.register(AnalyticsSettings)
class AnalyticsSettingsAdmin(admin.ModelAdmin):
    list_display = ["organization", "hot_lead_stage", "lead_won_stage", "lead_lost_stage", "stall_day_threshold"]
    autocomplete_fields = ["hot_lead_stage", "lead_won_stage", "lead_lost_stage"]