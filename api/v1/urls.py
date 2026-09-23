"""Canonical composition point for SHVYA's versioned HTTP API.

Externally visible paths are intentionally kept identical to the historical
ROOT_URLCONF registrations in config.urls.
"""

from django.urls import include, path
from rest_framework_simplejwt.views import TokenRefreshView

from apps.accounts.views import ThrottledTokenObtainPairView


urlpatterns = [
    path(
        "auth/token/",
        ThrottledTokenObtainPairView.as_view(),
        name="token_obtain_pair",
    ),
    path(
        "auth/token/refresh/",
        TokenRefreshView.as_view(),
        name="token_refresh",
    ),
    path("leads/", include("apps.crm.urls.api_v1")),
    path("copilot/", include("apps.copilot.urls.api_v1")),
    path("teams/", include("apps.teams.urls.api_v1")),
    path("call-intelligence/", include("apps.telephony.urls.api_v1")),
    path("ai-engagement/", include("apps.ai_engagement.urls.api_v1")),
]
