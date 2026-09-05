from django.urls import path

from apps.ai_engagement.views.playground import (
    PlaygroundAPIView,
)


urlpatterns = [
    path(
        "playground/",
        PlaygroundAPIView.as_view(),
        name="ai-playground",
    ),
]