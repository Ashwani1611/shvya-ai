from django.urls import path

from apps.analytics.views.web import (
    analytics_dashboard_view,
    analytics_settings_view,
    export_failed_template_leads_view,
    retry_failed_template_messages_view,
)

urlpatterns = [
    path("", analytics_dashboard_view, name="crm-analytics"),
    path("settings/", analytics_settings_view, name="crm-analytics-settings"),
    path("failed-whatsapp/retry/", retry_failed_template_messages_view, name="crm-analytics-failed-whatsapp-retry"),
    path("failed-whatsapp/export/", export_failed_template_leads_view, name="crm-analytics-failed-whatsapp-export"),
]
