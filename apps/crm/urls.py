from django.urls import path

from .views import (
    BulkMoveStageAPIView,
    LeadListAPIView,
    LeadUpsertAPIView,
)


urlpatterns = [
    path("upsert/", LeadUpsertAPIView.as_view(), name="lead-upsert"),
    path("", LeadListAPIView.as_view(), name="lead-list"),
    path(
        "bulk/move-stage/",
        BulkMoveStageAPIView.as_view(),
        name="bulk-move-stage",
    ),
]