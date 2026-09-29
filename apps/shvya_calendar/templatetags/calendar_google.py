from django import template

from apps.shvya_calendar.google_policy import organization_allows_platform_fallback
from apps.shvya_calendar.platform_google import is_platform_booking, platform_status

register = template.Library()


@register.simple_tag(takes_context=True)
def platform_google_status(context):
    """Expose effective, non-secret status for the signed-in organisation only."""
    request = context.get("request")
    user = getattr(request, "crm_user", None)
    organization_id = getattr(user, "organization_id", None)
    allowed = bool(
        organization_id and organization_allows_platform_fallback(organization_id)
    )
    status = platform_status()
    return {
        "enabled": status["enabled"] and allowed,
        "configured": status["configured"],
        "opted_in": allowed,
    }


@register.filter
def booking_google_organiser(booking):
    """Describe the persisted organiser, not today's hosting preference."""
    if not booking.google_event_id:
        return "Not assigned yet"
    if is_platform_booking(booking):
        return "SHVYA-managed Google Meet"
    return "Organisation Google account"
