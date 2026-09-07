"""Removal endpoint for abandoned or intentionally removed Hosted sessions."""

from django.http import Http404, JsonResponse
from django.views.decorators.http import require_POST

from apps.crm.decorators import crm_login_required
from apps.organizations.features import is_hosted_account_enabled

from .hosted_tasks import remove_hosted_session_task
from .models import WhatsAppAccount


def _organization(request):
    user = getattr(request, "crm_user", None)
    organization = getattr(user, "organization", None)
    if not organization or not is_hosted_account_enabled(organization):
        raise Http404
    return organization


@crm_login_required
@require_POST
def hosted_session_remove_view(request, account_id):
    account = WhatsAppAccount.objects.filter(
        id=account_id,
        organization=_organization(request),
        connection_type="hosted",
        is_active=True,
    ).first()
    if not account:
        raise Http404

    # Hide it from the tenant immediately. The task intentionally looks up
    # inactive Hosted accounts too, so gateway logout/LocalAuth cleanup still
    # happens after this state transition.
    account.status = WhatsAppAccount.Status.DISCONNECTED
    account.is_active = False
    account.save(update_fields=["status", "is_active", "updated_at"])
    remove_hosted_session_task.delay(str(account.id))

    return JsonResponse(
        {
            "ok": True,
            "status": "Removed",
            "account_id": str(account.id),
        }
    )
