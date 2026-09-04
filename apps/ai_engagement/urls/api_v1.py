from django.urls import path

from apps.ai_engagement.views.document_views import (
    DocumentDetailAPIView,
    DocumentListAPIView,
    DocumentReindexAPIView,
    KnowledgeSourceDetailAPIView,
    KnowledgeSourceListAPIView,
)
from apps.ai_engagement.views.faq import (
    FAQDetailView,
    FAQListView,
)
from apps.ai_engagement.views.org_info import OrgInfoView

from apps.ai_engagement.views.playground import (
    PlaygroundAPIView,
)

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
    path(
        "documents/",
        DocumentListAPIView.as_view(),
        name="ai-document-list",
    ),
    path(
        "documents/<int:document_id>/",
        DocumentDetailAPIView.as_view(),
        name="ai-document-detail",
    ),
    path(
        "documents/<int:document_id>/reindex/",
        DocumentReindexAPIView.as_view(),
        name="ai-document-reindex",
    ),
    path(
        "sources/",
        KnowledgeSourceListAPIView.as_view(),
        name="ai-knowledge-source-list",
    ),
    path(
        "sources/<int:source_id>/",
        KnowledgeSourceDetailAPIView.as_view(),
        name="ai-knowledge-source-detail",
    ),
    path(
        "playground/",
        PlaygroundAPIView.as_view(),
        name="ai-playground",
    ),
]