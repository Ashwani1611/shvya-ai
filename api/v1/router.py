from django.urls import path, include

urlpatterns = [
    path("crm/",        include("apps.crm.urls.api_v1")),
    path("triggers/",   include("apps.triggers.urls.api_v1")),
    path("telephony/",  include("apps.telephony.urls.api_v1")),
    path("knowledge/",  include("apps.knowledge.urls.api_v1")),
]
