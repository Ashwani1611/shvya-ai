from django.urls import path

from apps.telephony.views.api import (
    MobileReminderCollectionView,
    MobileReminderActionView,
    MobileLeadCollectionView,
    CallAnalyticsView,
    CallCollectionView,
    CallDispositionCollectionView,
    CallEventView,
    CallFollowUpView,
    CallMediaView,
    CallNotesView,
    CallSettingsView,
    DeviceHeartbeatView,
    DeviceRegistrationView,
)

urlpatterns = [
    path("reminders/", MobileReminderCollectionView.as_view(), name="call-mobile-reminders"),
    path("reminders/<uuid:reminder_id>/action/", MobileReminderActionView.as_view(), name="call-mobile-reminder-action"),
    path("leads/", MobileLeadCollectionView.as_view(), name="call-mobile-lead-create"),
    path("devices/register/", DeviceRegistrationView.as_view(), name="call-device-register"),
    path("devices/heartbeat/", DeviceHeartbeatView.as_view(), name="call-device-heartbeat"),
    path("events/", CallEventView.as_view(), name="call-event-ingest"),
    path("calls/", CallCollectionView.as_view(), name="call-api-list"),
    path("calls/<uuid:call_id>/media/", CallMediaView.as_view(), name="call-api-media"),
    path("calls/<uuid:call_id>/notes/", CallNotesView.as_view(), name="call-api-notes"),
    path("calls/<uuid:call_id>/follow-up/", CallFollowUpView.as_view(), name="call-api-follow-up"),
    path("analytics/", CallAnalyticsView.as_view(), name="call-api-analytics"),
    path("dispositions/", CallDispositionCollectionView.as_view(), name="call-api-dispositions"),
    path("settings/", CallSettingsView.as_view(), name="call-api-settings"),
]
