from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render

from apps.crm.authentication import crm_login_required
from apps.integrations.models import WebhookConfiguration
from apps.integrations.services.webhook import validate_webhook_url


@crm_login_required
def webhook_view(request):
    organization = request.crm_user.organization
    webhook, _ = WebhookConfiguration.objects.get_or_create(
        organization=organization,
    )

    if request.method == "POST":
        endpoint_url = request.POST.get("endpoint_url", "").strip()
        raw_secret = request.POST.get("secret", "")
        is_enabled = request.POST.get("is_enabled") == "on"
        errors = []

        try:
            endpoint_url = validate_webhook_url(endpoint_url)
        except ValidationError as exc:
            errors.extend(exc.messages)

        if raw_secret and len(raw_secret) < 8:
            errors.append("Webhook secret must be at least 8 characters long.")

        secret_will_exist = bool(raw_secret) or webhook.has_secret
        if is_enabled and not secret_will_exist:
            errors.append("Add a webhook secret before enabling the webhook.")

        if errors:
            for error in errors:
                messages.error(request, error)
        else:
            webhook.endpoint_url = endpoint_url
            webhook.is_enabled = is_enabled

            if raw_secret:
                webhook.set_secret(raw_secret)

            webhook.save()
            messages.success(
                request,
                "Webhook configuration saved successfully.",
            )
            return redirect("crm-connect-hub-webhook")

    recent_deliveries = webhook.deliveries.only(
        "id",
        "event_type",
        "status",
        "attempt_count",
        "response_status",
        "created_at",
        "delivered_at",
    )[:8]

    return render(
        request,
        "integrations/webhook.html",
        {
            "webhook": webhook,
            "recent_deliveries": recent_deliveries,
        },
    )
