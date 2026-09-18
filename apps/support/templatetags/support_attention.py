"""Bootstrap the support indicator only in the authenticated dashboard shell."""
from django import template
from django.urls import reverse

from apps.support.access import customer_authorized
from apps.support.attention import attention_count

register = template.Library()


@register.simple_tag(takes_context=True)
def support_attention_state(context):
    request = context.get("request")
    if getattr(request, "shvya_session_area", None) != "dashboard":
        return None
    user = getattr(request, "user", None)
    if not customer_authorized(user):
        return None
    return {
        "count": attention_count(user),
        "endpoint": reverse("support-client:attention"),
        "portal": reverse("support-client:list"),
    }
