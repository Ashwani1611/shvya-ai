from django.urls import path

from apps.copilot.views.web import copilot_dashboard


urlpatterns = [
    path("", copilot_dashboard, name="crm-copilot"),
]
