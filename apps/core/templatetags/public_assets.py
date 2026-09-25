from urllib.parse import quote

from django import template
from django.conf import settings
from django.templatetags.static import static

register = template.Library()


@register.simple_tag
def public_asset(path):
    """Return the external heavy-asset URL, with a normal static fallback."""
    relative = str(path or "").lstrip("/")
    base = str(
        getattr(settings, "PUBLIC_ASSET_BASE_URL", "") or ""
    ).strip().rstrip("/")
    if not base:
        return static(relative)
    return f"{base}/{quote(relative, safe='/@:+-._~')}"
