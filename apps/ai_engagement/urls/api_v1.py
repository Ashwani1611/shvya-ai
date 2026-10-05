from django.urls import path

from apps.ai_engagement.views.brain_bundle import OrganizationAIBrainBundleView
from apps.ai_engagement.views.dashboard_knowledge_delete import (
    dashboard_knowledge_delete,
)
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
    PlaygroundAPIView, PlaygroundFileAPIView,
)

from apps.ai_engagement.views.shared_files import instagram_shared_file

urlpatterns = [
    path("brain-bundle/", OrganizationAIBrainBundleView.as_view(), name="ai-brain-bundle"),
    path("provider-files/<str:token>/", instagram_shared_file, {"provider_fetch": True}, name="ai-instagram-provider-file"),
    path("shared-files/<str:token>/", instagram_shared_file, name="ai-instagram-shared-file"),
    path("playground/files/<int:document_id>/", PlaygroundFileAPIView.as_view(), name="ai-playground-file"),
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
        "dashboard/knowledge/<str:kind>/<int:item_id>/delete/",
        dashboard_knowledge_delete,
        name="ai-dashboard-knowledge-delete",
    ),
    path(
        "playground/",
        PlaygroundAPIView.as_view(),
        name="ai-playground",
    ),
]
