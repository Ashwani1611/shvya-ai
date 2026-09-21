from django.urls import path

from apps.telephony.views.dashboard import (
    call_intelligence_dashboard,
    download_android_app,
    update_call_settings,
)

urlpatterns = [
    path("", call_intelligence_dashboard, name="call-intelligence-dashboard"),
    path("download/android/", download_android_app, name="call-intelligence-download"),
    path("settings/", update_call_settings, name="call-intelligence-settings"),
]
