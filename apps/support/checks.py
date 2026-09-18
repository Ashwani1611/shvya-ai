from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.core.checks import Error, Warning, register

from .storage import support_root


@register()
def support_checks(app_configs, **kwargs):
    issues = []
    root = support_root().resolve()
    static_roots = [getattr(settings, "STATIC_ROOT", None)]
    for item in getattr(settings, "STATICFILES_DIRS", []):
        static_roots.append(item[1] if isinstance(item, (list, tuple)) else item)
    if any(value and root.is_relative_to(Path(value).resolve()) for value in static_roots):
        issues.append(Error("Support storage must not be inside static assets.", id="support.E001"))
    url = getattr(settings, "SUPPORT_PUBLIC_BASE_URL", "").rstrip("/")
    try:
        parsed = urlsplit(url)
        valid_origin = bool(parsed.scheme == "https" and parsed.hostname and not parsed.path
                            and not parsed.query and not parsed.fragment and not parsed.username)
    except ValueError:
        valid_origin = False
    if not valid_origin:
        issues.append(Warning("Set SUPPORT_PUBLIC_BASE_URL to the canonical HTTPS origin.", id="support.W001"))
    if not root.is_relative_to(Path(settings.MEDIA_ROOT).resolve()):
        issues.append(Warning("Custom support storage needs a persistent shared mount.", id="support.W002"))
    if getattr(settings, "SUPPORT_REQUIRE_SCANNER", False) and not getattr(settings, "SUPPORT_ATTACHMENT_SCANNER", ""):
        issues.append(Error("A support attachment scanner is required but not configured.", id="support.E002"))
    return issues
