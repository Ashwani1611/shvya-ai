from django.urls import path, include

# NOTE: not wired into ROOT_URLCONF (see config/urls.py, which
# includes each app's urls.api_v1 directly). Kept for reference /
# future consolidation only.

urlpatterns = [
    path("crm/",        include("apps.crm.urls.api_v1")),
    path("triggers/",   include("apps.triggers.urls.api_v1")),
    path("telephony/",  include("apps.telephony.urls.api_v1")),
    path("knowledge/",  include("apps.ai_engagement.urls.api_v1")),
]
