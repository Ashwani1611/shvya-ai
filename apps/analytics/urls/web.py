from django.urls import path

from apps.analytics.views.web import analytics_dashboard_view, analytics_settings_view

urlpatterns = [
    path("", analytics_dashboard_view, name="crm-analytics"),
    path("settings/", analytics_settings_view, name="crm-analytics-settings"),
]