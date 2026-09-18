from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_GET
from apps.crm.decorators import crm_login_required
from apps.crm.models import Lead
from services.crm.lead_filter_service import accessible_pipelines
from services.crm.lead_chat import lead_chat_url


@crm_login_required
@require_GET
def lead_whatsapp(request, lead_id):
    lead = get_object_or_404(Lead.objects.select_related("pipeline"), pk=lead_id,
                            organization=request.crm_user.organization,
                            pipeline__in=accessible_pipelines(request.crm_user))
    try:
        return redirect(lead_chat_url(lead))
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect("crm-dashboard")
