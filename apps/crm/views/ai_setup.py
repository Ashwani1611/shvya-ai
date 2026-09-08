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
from apps.crm.models import Pipeline
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

        if action == "save_pipeline_ai":
            pipeline_id = request.POST.get("pipeline_id", "")
            pipeline = Pipeline.objects.filter(
                id=pipeline_id,
                organization=organization,
                is_active=True,
            ).first()
            if pipeline is None:
                messages.error(request, "Pipeline not found.")
            else:
                pipeline.ai_enabled = request.POST.get("ai_enabled") == "on"
                pipeline.save(update_fields=["ai_enabled", "updated_at"])
                messages.success(
                    request,
                    f"AI engagement for {pipeline.name} is now "
                    f"{'on' if pipeline.ai_enabled else 'off'}.",
                )
            return redirect("crm-knowledge-base-ai-setup")

        if action == "upload_guided_file":
            uploaded_file = request.FILES.get("file")
            instruction = request.POST.get("share_instruction", "").strip()
            if uploaded_file is None or not instruction:
                messages.error(request, "Choose a file and describe when the AI should send it.")
                return redirect("crm-knowledge-base-ai-setup")
            try:
                _source, document = source_service.create_file_source(
                    organization=organization,
                    uploaded_file=uploaded_file,
                    name=request.POST.get("name", "").strip(),
                )
                document.share_instruction = instruction
                document.save(update_fields=["share_instruction", "updated_at"])
                ingest_and_index_document.delay(
                    document_id=document.id,
                    organization_id=organization.id,
                )
            except KnowledgeSourceServiceError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "AI-guided file added and processing started.")
            return redirect("crm-knowledge-base-ai-setup")

        if action == "update_guided_file":
            instruction = request.POST.get("share_instruction", "").strip()
            document = Document.objects.filter(
                id=request.POST.get("document_id", ""),
                organization=organization,
            ).exclude(file="").first()
            if document is None or not instruction:
                messages.error(request, "File and sending instruction are required.")
            else:
                document.share_instruction = instruction
                document.save(update_fields=["share_instruction", "updated_at"])
                messages.success(request, "File instruction updated.")
            return redirect("crm-knowledge-base-ai-setup")

        if action == "delete_guided_file":
            document = Document.objects.filter(
                id=request.POST.get("document_id", ""),
                organization=organization,
            ).exclude(file="").first()
            if document is None:
                messages.error(request, "File not found.")
            else:
                source_service.delete_document(document=document)
                messages.success(request, "File and its indexed data were permanently deleted.")
            return redirect("crm-knowledge-base-ai-setup")

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
            "guided_documents": documents.exclude(share_instruction=""),
            "pipelines": Pipeline.objects.filter(
                organization=organization,
                is_active=True,
            ).order_by("name"),
            "supported_file_extensions": (
                KnowledgeSourceService()
                .ingestion_service
                .SUPPORTED_FILE_EXTENSIONS
            ),
        },
    )
