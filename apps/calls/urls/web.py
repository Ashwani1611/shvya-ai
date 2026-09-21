from django.urls import path

from apps.calls.views.web import (
    call_intelligence_dashboard,
    call_intelligence_download,
    call_intelligence_settings,
)

urlpatterns = [
    path("", call_intelligence_dashboard, name="call-intelligence-dashboard"),
    path("download/", call_intelligence_download, name="call-intelligence-download"),
    path("settings/", call_intelligence_settings, name="call-intelligence-settings"),
]
