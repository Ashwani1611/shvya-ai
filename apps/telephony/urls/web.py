from django.urls import path

from apps.telephony.views.dashboard import (
    call_intelligence_dashboard,
    call_analysis_status,
    create_call_lead,
    remove_call_device,
    download_android_app,
    post_call_action,
    save_call_disposition,
    update_call_settings,
)

urlpatterns = [
    path("devices/<uuid:device_id>/remove/", remove_call_device, name="call-intelligence-device-remove"),
    path("", call_intelligence_dashboard, name="call-intelligence-dashboard"),
    path("download/android/", download_android_app, name="call-intelligence-download"),
    path("calls/<uuid:call_id>/action/", post_call_action, name="call-intelligence-call-action"),
    path("calls/<uuid:call_id>/lead/", create_call_lead, name="call-intelligence-call-lead"),
    path("calls/<uuid:call_id>/status/", call_analysis_status, name="call-intelligence-call-status"),
    path("dispositions/save/", save_call_disposition, name="call-intelligence-disposition-save"),
    path("settings/", update_call_settings, name="call-intelligence-settings"),
]
