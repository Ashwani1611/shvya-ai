from __future__ import annotations

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render

from apps.ai_engagement.models import Document, KnowledgeSource
from apps.ai_engagement.services.ai_brain_setup import save_ai_brain_configuration
from apps.ai_engagement.services.knowledge_file_security import MAX_UPLOAD_BYTES
from apps.ai_engagement.services.knowledge_source import (
    KnowledgeSourceService,
    KnowledgeSourceServiceError,
)
from apps.ai_engagement.services.playbook import parse_playbook
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
            data = {
                "organization_name": request.POST.get("organization_name", "").strip(),
                "about": request.POST.get("about", "").strip(),
                "bot_languages": request.POST.get("bot_languages", "").strip(),
                "ai_playbook": request.POST.get("ai_playbook", "").strip(),
            }
            try:
                if not data["about"]:
                    raise OrgInfoServiceError("Describe what your company does.")
                saved = save_ai_brain_configuration(
                    organization=organization,
                    data=data,
                    urls=request.POST.getlist("knowledge_urls"),
                    uploaded_file=request.FILES.get("knowledge_file"),
                )
            except (OrgInfoServiceError, KnowledgeSourceServiceError) as exc:
                if request.headers.get("Accept") == "application/json":
                    return JsonResponse({"saved": False, "error": str(exc)}, status=400)
                messages.error(request, str(exc))
                return _render_ai_setup(request, organization, form_values=data, status=400)
            if request.headers.get("Accept") == "application/json":
                saved.refresh_from_db()
                return JsonResponse({"saved": True, "updated_at": saved.updated_at.isoformat(), "ai_playbook": saved.ai_playbook})
            messages.success(request, "AI Brain saved successfully.")
            return redirect("crm-knowledge-base-ai-setup")

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

    return _render_ai_setup(request, organization)


def _render_ai_setup(request, organization, *, form_values=None, status=200):
    org_info = OrgInfoService().get_or_create(organization=organization)
    sources, documents = _get_knowledge_data(organization)
    values = form_values if form_values is not None else {
        "organization_name": organization.name,
        "about": org_info.about,
        "bot_languages": org_info.bot_languages,
        "ai_playbook": org_info.ai_playbook,
    }
    sections = parse_playbook(values["ai_playbook"])
    return render(
        request,
        "crm/knowledge_base/ai_setup.html",
        {
            "org_info": org_info,
            "organization": organization,
            "form_values": values,
            "playbook_needs_criteria": bool(
                sections["qualification_questions"] and not sections["qualification_criteria"]
            ),
            "pending_urls": request.POST.getlist("knowledge_urls") or [""],
            "knowledge_sources": sources.filter(source_type=KnowledgeSource.SourceType.URL),
            "knowledge_documents": documents.filter(share_instruction="").exclude(file=""),
            "guided_documents": documents.exclude(share_instruction="").exclude(file=""),
            "supported_file_extensions": sorted(
                KnowledgeSourceService().ingestion_service.SUPPORTED_FILE_EXTENSIONS
            ),
            "knowledge_max_upload_bytes": MAX_UPLOAD_BYTES,
        },
        status=status,
    )
