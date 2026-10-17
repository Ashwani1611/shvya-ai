"""Superadmin AI Brain editor: URL-authorized tenant, canonical services, CSRF."""
from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.ai_engagement.models import Document, FAQ, KnowledgeSource, OrgInfo
from apps.ai_engagement.services.faq import FAQService, FAQServiceError
from apps.ai_engagement.services.file_sharing import FileSharingService, FileSharingError
from apps.ai_engagement.services.knowledge_source import KnowledgeSourceServiceError
from apps.ai_engagement.services.org_info import OrgInfoServiceError
from apps.ai_engagement.services.knowledge_repair import plan_repair, request_repair, KnowledgeRepairError
from apps.organizations.models import Organization
from apps.crm.models import Pipeline, Stage
from .ai_brain_forms import AIBrainForm, KnowledgeURLForm, KnowledgeUploadForm, FAQForm
from .ai_brain_services import AI_FIELDS, StaleAIConfiguration, revision_token, check_revision, save_brain, add_url, add_file, audit
from .views_flat import superuser_required


@superuser_required
@require_http_methods(["GET", "POST"])
def organization_ai_brain(request, organization_id):
    organization = get_object_or_404(Organization, pk=organization_id)
    info = OrgInfo.objects.filter(organization=organization).first()
    initial = {field: getattr(info, field) for field in AI_FIELDS} if info else {"ai_enabled": True, "bump_up_enabled": True, "bump_up_count": 2}
    initial["revision"] = revision_token(organization, info)
    forms = {"brain": AIBrainForm(initial=initial), "url": KnowledgeURLForm(), "upload": KnowledgeUploadForm(), "faq": FAQForm()}
    error, status = "", 200
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action in forms:
                forms[action] = type(forms[action])(request.POST, request.FILES)
                if not forms[action].is_valid():
                    status = 400
                else:
                    data = forms[action].cleaned_data
                    if action == "brain":
                        save_brain(request, organization, data)
                    elif action == "url":
                        add_url(request, organization, data)
                    elif action == "upload":
                        add_file(request, organization, data)
                    elif action == "faq":
                        with transaction.atomic():
                            faq_id = request.POST.get("faq_id")
                            if faq_id:
                                faq = get_object_or_404(FAQ.objects.select_for_update(), pk=faq_id, organization=organization)
                                check_revision(request.POST.get("revision", ""), organization, faq, "faq")
                                FAQService().update(organization=organization, faq_id=faq.pk, data=data)
                            else:
                                faq = FAQService().create(organization=organization, **data)
                            audit(request, organization, "faq_saved", faq_id=faq.pk)
            elif action == "file_instruction":
                with transaction.atomic():
                    document = get_object_or_404(Document.objects.select_for_update(), pk=request.POST.get("document_id"), organization=organization)
                    check_revision(request.POST.get("revision", ""), organization, document, "document")
                    instruction = request.POST.get("share_instruction", "").strip()
                    if len(instruction) > 10000 or not document.file:
                        raise ValueError("Choose an uploaded file and use at most 10,000 characters.")
                    document.share_instruction = instruction
                    document.save(update_fields=["share_instruction", "updated_at"])
                    if instruction:
                        FileSharingService.prepare_uploaded_file(document=document)
                    audit(request, organization, "file_instruction_saved", document_id=document.pk)
            elif action in {"pipeline_ai", "stage_ai"}:
                with transaction.atomic():
                    if action == "pipeline_ai":
                        target = get_object_or_404(Pipeline.objects.select_for_update(), pk=request.POST.get("target_id"), organization=organization)
                    else:
                        target = get_object_or_404(Stage.objects.select_for_update(), pk=request.POST.get("target_id"), pipeline__organization=organization)
                    check_revision(request.POST.get("revision", ""), organization, target, action)
                    field = "ai_enabled" if action == "pipeline_ai" else "ai_on"
                    enabled = request.POST.get("ai_enabled") == "on"
                    setattr(target, field, enabled)
                    target.save(update_fields=[field, "updated_at"])
                    audit(request, organization, action, target_id=str(target.pk), enabled=enabled)
            elif action == "repair":
                document = get_object_or_404(Document, pk=request.POST.get("document_id"), organization=organization)
                with transaction.atomic():
                    request_repair(organization=organization, document_id=document.pk, expected_fingerprint=request.POST.get("fingerprint", ""))
                    audit(request, organization, "knowledge_repair_requested", document_id=document.pk)
            else:
                raise ValueError("Choose a supported AI Brain action.")
            if status == 200:
                messages.success(request, "Saved for " + organization.name + (". Knowledge processing is queued; check its status below." if action in {"url", "upload", "repair"} else "."))
                return redirect(reverse("superadmin-organization-ai-brain", args=[organization.pk]))
        except StaleAIConfiguration as exc:
            error, status = str(exc), 409
        except (OrgInfoServiceError, KnowledgeSourceServiceError, FAQServiceError, FileSharingError, KnowledgeRepairError, ValueError) as exc:
            error, status = str(exc), 400
    documents = Paginator(Document.objects.filter(organization=organization).annotate(
        chunk_count=Count("chunks", filter=Q(chunks__organization=organization, chunks__is_active=True)),
        indexed_count=Count("chunks", filter=Q(chunks__organization=organization, chunks__is_active=True, chunks__embedding__isnull=False)),
    ).order_by("-updated_at"), 20).get_page(request.GET.get("documents_page"))
    for document in documents:
        document.edit_revision = revision_token(organization, document, "document")
        # Repair plans inspect bounded chunk state only on an explicit preview request.
        document.repair_plan = None
        document.edit_instruction = document.share_instruction
        if status != 200 and request.POST.get("action") == "file_instruction" and request.POST.get("document_id") == str(document.pk):
            document.edit_instruction = request.POST.get("share_instruction", "")
            document.edit_revision = request.POST.get("revision", "")
    repair_plan = None
    if request.GET.get("repair_document"):
        document = get_object_or_404(Document, organization=organization, pk=request.GET["repair_document"])
        repair_plan = plan_repair(organization=organization, document_id=document.pk)
    faqs = Paginator(FAQ.objects.filter(organization=organization).order_by("-updated_at"), 20).get_page(request.GET.get("faqs_page"))
    for faq in faqs:
        faq.edit_revision = revision_token(organization, faq, "faq")
    pipelines = list(Pipeline.objects.filter(organization=organization).prefetch_related("stages").order_by("name"))
    for pipeline in pipelines:
        pipeline.edit_revision = revision_token(organization, pipeline, "pipeline_ai")
        for stage in pipeline.stages.all():
            stage.edit_revision = revision_token(organization, stage, "stage_ai")
    response = render(request, "superadmin/ai_brain.html", {
        "organization": organization, "brain_info": info, "brain_form": forms["brain"],
        "url_form": forms["url"], "upload_form": forms["upload"], "faq_form": forms["faq"],
        "documents": documents, "faqs": faqs,
        "sources": KnowledgeSource.objects.filter(organization=organization, source_type="url").order_by("-updated_at")[:100],
        "error": error, "repair_plan": repair_plan, "pipelines": pipelines,
        "editing_faq_id": request.POST.get("faq_id", "") if request.method == "POST" and request.POST.get("action") == "faq" else "",
        "editing_faq_revision": request.POST.get("revision", ""),
        "model_routing_info": info,
    }, status=status)
    response["Cache-Control"] = "private, no-store"
    return response
