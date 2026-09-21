from django.urls import path

from apps.calls.views.api import (
    CallBootstrapAPIView,
    CallDetailAPIView,
    CallEventIngestAPIView,
    CallFollowUpAPIView,
    CallListAPIView,
    CallNotesAPIView,
    DeviceHeartbeatAPIView,
    DeviceRegisterAPIView,
)

urlpatterns = [
    path("bootstrap/", CallBootstrapAPIView.as_view(), name="call-intelligence-bootstrap"),
    path("devices/register/", DeviceRegisterAPIView.as_view(), name="call-device-register"),
    path("devices/heartbeat/", DeviceHeartbeatAPIView.as_view(), name="call-device-heartbeat"),
    path("events/", CallEventIngestAPIView.as_view(), name="call-event-ingest"),
    path("calls/", CallListAPIView.as_view(), name="call-intelligence-call-list"),
    path("calls/<uuid:call_id>/", CallDetailAPIView.as_view(), name="call-intelligence-call-detail"),
    path("calls/<uuid:call_id>/notes/", CallNotesAPIView.as_view(), name="call-intelligence-call-notes"),
    path("calls/<uuid:call_id>/follow-up/", CallFollowUpAPIView.as_view(), name="call-intelligence-call-follow-up"),
]
