from django.urls import path

from apps.triggers.views import trigger_views as views

urlpatterns = [
    path("", views.dashboard, name="crm-smart-triggers"),
    path("rules/", views.rules_api, name="smart-trigger-rules"),
    path("rules/<uuid:rule_id>/", views.rules_api, name="smart-trigger-rule"),
    path("reorder/", views.reorder_api, name="smart-trigger-reorder"),
    path("history/", views.history_api, name="smart-trigger-history"),
]
