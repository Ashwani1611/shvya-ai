from __future__ import annotations

from django.contrib import messages
from django.shortcuts import redirect, render

from apps.ai_engagement.models import Document, KnowledgeSource
from apps.ai_engagement.services.knowledge_source import (
    KnowledgeSourceService,
    KnowledgeSourceServiceError,
)
from apps.ai_engagement.services.org_info import (
    OrgInfoService,
    OrgInfoServiceError,
)
from apps.ai_engagement.tasks import (
    ingest_and_index_document,
    ingest_and_index_url_source,
    reindex_document_embeddings,
)
from apps.crm.authentication import crm_login_required


def _get_knowledge_data(organization):
    sources = (
        KnowledgeSource.objects
        .filter(
            organization=organization,
        )
        .order_by(
            "-updated_at",
        )
    )

    documents = (
        Document.objects
        .filter(
            organization=organization,
        )
        .order_by(
            "-updated_at",
        )
    )

    return sources, documents


@crm_login_required
def ai_setup_view(request):
    """
    Organization-level AI Setup page.

    Configuration is handled through OrgInfoService.
    Knowledge sources use KnowledgeSourceService and the
    existing asynchronous AI Engagement tasks.
    """

    user = request.crm_user
    organization = user.organization

    org_info_service = OrgInfoService()
    source_service = KnowledgeSourceService()

    if request.method == "POST":

        action = request.POST.get(
            "action",
            "save_settings",
        )

        # =========================================================
        # ORGANIZATION AI SETTINGS
        # =========================================================

        if action == "save_settings":

            about = request.POST.get(
                "about",
                "",
            )

            bot_languages = request.POST.get(
                "bot_languages",
                "",
            )

            qualification_requirements = request.POST.get(
                "qualification_requirements",
                "",
            )

            engagement_instructions = request.POST.get(
                "engagement_instructions",
                "",
            )

            ai_enabled = (
                request.POST.get("ai_enabled")
                == "on"
            )

            bump_up_enabled = (
                request.POST.get("bump_up_enabled")
                == "on"
            )

            bump_up_count_raw = request.POST.get(
                "bump_up_count",
                "0",
            )

            try:
                bump_up_count = int(
                    bump_up_count_raw
                )

            except (
                TypeError,
                ValueError,
            ):
                messages.error(
                    request,
                    "Bump-up count must be a non-negative integer.",
                )

                return redirect(
                    "crm-knowledge-base-ai-setup"
                )

            try:
                org_info_service.update(
                    organization=organization,
                    data={
                        "about": about,
                        "bot_languages": bot_languages,
                        "qualification_requirements": (
                            qualification_requirements
                        ),
                        "engagement_instructions": (
                            engagement_instructions
                        ),
                        "ai_enabled": ai_enabled,
                        "bump_up_enabled": bump_up_enabled,
                        "bump_up_count": bump_up_count,
                    },
                )

            except OrgInfoServiceError as exc:
                messages.error(
                    request,
                    str(exc),
                )

                return redirect(
                    "crm-knowledge-base-ai-setup"
                )

            messages.success(
                request,
                "AI settings saved successfully.",
            )

            return redirect(
                "crm-knowledge-base-ai-setup"
            )

        # =========================================================
        # ADD URL KNOWLEDGE SOURCE
        # =========================================================

        if action == "add_url_source":

            url = (
                request.POST.get(
                    "url",
                    "",
                )
                .strip()
            )

            name = (
                request.POST.get(
                    "name",
                    "",
                )
                .strip()
            )

            if not url:

                messages.error(
                    request,
                    "Please enter a website URL.",
                )

                return redirect(
                    "crm-knowledge-base-ai-setup"
                )

            try:
                source = source_service.create_url_source(
                    organization=organization,
                    url=url,
                    name=name,
                )

                ingest_and_index_url_source.delay(
                    source_id=source.id,
                    organization_id=organization.id,
                )

            except KnowledgeSourceServiceError as exc:

                messages.error(
                    request,
                    str(exc),
                )

                return redirect(
                    "crm-knowledge-base-ai-setup"
                )

            messages.success(
                request,
                "Knowledge source added. Processing has started.",
            )

            return redirect(
                "crm-knowledge-base-ai-setup"
            )

        # =========================================================
        # UPLOAD FILE KNOWLEDGE SOURCE
        # =========================================================

        if action == "upload_file":

            uploaded_file = request.FILES.get(
                "file"
            )

            name = (
                request.POST.get(
                    "name",
                    "",
                )
                .strip()
            )

            if uploaded_file is None:

                messages.error(
                    request,
                    "Please choose a file to upload.",
                )

                return redirect(
                    "crm-knowledge-base-ai-setup"
                )

            try:

                source, document = (
                    source_service.create_file_source(
                        organization=organization,
                        uploaded_file=uploaded_file,
                        name=name,
                    )
                )

                ingest_and_index_document.delay(
                    document_id=document.id,
                    organization_id=organization.id,
                )

            except KnowledgeSourceServiceError as exc:

                messages.error(
                    request,
                    str(exc),
                )

                return redirect(
                    "crm-knowledge-base-ai-setup"
                )

            messages.success(
                request,
                "File uploaded. Processing has started.",
            )

            return redirect(
                "crm-knowledge-base-ai-setup"
            )

        # =========================================================
        # DEACTIVATE KNOWLEDGE SOURCE
        # =========================================================

        if action == "deactivate_source":

            source_id = request.POST.get(
                "source_id",
                "",
            )

            try:
                source = (
                    KnowledgeSource.objects
                    .get(
                        id=source_id,
                        organization=organization,
                    )
                )

                source_service.deactivate_source(
                    source=source,
                )

            except (
                KnowledgeSource.DoesNotExist,
                KnowledgeSourceServiceError,
            ) as exc:

                messages.error(
                    request,
                    str(exc),
                )

                return redirect(
                    "crm-knowledge-base-ai-setup"
                )

            messages.success(
                request,
                "Knowledge source deactivated.",
            )

            return redirect(
                "crm-knowledge-base-ai-setup"
            )

        # =========================================================
        # REINDEX DOCUMENT
        # =========================================================

        if action == "reindex_document":

            document_id = request.POST.get(
                "document_id",
                "",
            )

            try:
                document = (
                    Document.objects
                    .get(
                        id=document_id,
                        organization=organization,
                    )
                )

                if (
                    document.processing_status
                    != Document.ProcessingStatus.COMPLETED
                ):
                    raise KnowledgeSourceServiceError(
                        "Only a completed document can be re-indexed."
                    )

                reindex_document_embeddings.delay(
                    document_id=document.id,
                    organization_id=organization.id,
                )

            except (
                Document.DoesNotExist,
                KnowledgeSourceServiceError,
            ) as exc:

                messages.error(
                    request,
                    str(exc),
                )

                return redirect(
                    "crm-knowledge-base-ai-setup"
                )

            messages.success(
                request,
                "Document re-indexing has been queued.",
            )

            return redirect(
                "crm-knowledge-base-ai-setup"
            )

        messages.error(
            request,
            "Unknown AI Setup action.",
        )

        return redirect(
            "crm-knowledge-base-ai-setup"
        )

    org_info = org_info_service.get_or_create(
        organization=organization,
    )

    sources, documents = _get_knowledge_data(
        organization,
    )

    return render(
        request,
        "crm/knowledge_base/ai_setup.html",
        {
            "org_info": org_info,
            "organization": organization,
            "knowledge_sources": sources,
            "knowledge_documents": documents,
            "supported_file_extensions": (
                KnowledgeSourceService()
                .ingestion_service
                .SUPPORTED_FILE_EXTENSIONS
            ),
        },
    )