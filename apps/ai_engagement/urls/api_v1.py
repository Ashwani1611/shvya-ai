from django.urls import path

from apps.ai_engagement.views.faq import (
    FAQDetailView,
    FAQListView,
)
from apps.ai_engagement.views.org_info import OrgInfoView


urlpatterns = [
    path(
        "org-info/",
        OrgInfoView.as_view(),
        name="ai-org-info",
    ),
    path(
        "faqs/",
        FAQListView.as_view(),
        name="ai-faq-list",
    ),
    path(
        "faqs/<int:faq_id>/",
        FAQDetailView.as_view(),
        name="ai-faq-detail",
    ),
]