from django import template

from apps.shvya_calendar.platform_google import platform_status

register = template.Library()


@register.simple_tag
def platform_google_status():
    """Expose configuration state only, never Google credentials or identity."""
    status = platform_status()
    return {"enabled": status["enabled"], "configured": status["configured"]}
