from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods
from apps.channels.instagram_models import InstagramConversation
from apps.crm.decorators import crm_login_required
from services.crm.lead_filter_service import accessible_pipelines
from services.channels.instagram_leads import link_instagram_lead


@crm_login_required
@require_http_methods(["GET", "POST"])
def instagram_link_lead(request, conversation_id):
    conversation = get_object_or_404(InstagramConversation, pk=conversation_id,
                                    organization=request.crm_user.organization,
                                    account__organization=request.crm_user.organization)
    error = ""
    if request.method == "POST":
        try:
            if request.POST.get("confirmed") != "yes":
                raise ValidationError("Confirm that this phone number belongs to this Instagram participant.")
            link_instagram_lead(user=request.crm_user, conversation_id=conversation.pk,
                                phone=request.POST.get("phone", ""), name=request.POST.get("name", ""),
                                pipeline_id=request.POST.get("pipeline", ""))
            messages.success(request, "Instagram conversation linked to the lead.")
            return redirect("crm-instagram-chat-detail", conversation_id=str(conversation.pk))
        except ValidationError as exc:
            error = " ".join(exc.messages)
        except (IntegrityError, ValueError):
            error = "The lead changed while saving. Check the phone and pipeline, then retry."
    return render(request, "channels/instagram_link_lead.html", {
        "conversation": conversation, "pipelines": accessible_pipelines(request.crm_user), "error": error,
    }, status=400 if error else 200)
