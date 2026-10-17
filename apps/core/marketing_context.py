"""Public marketing metadata and optional Google measurement configuration."""
import json
import re
from django.conf import settings

PUBLIC_ROUTES = {
    "home", "features", "pricing", "docs", "docs-article", "book_call",
    "services", "product_suite", "privacy_policy", "terms_conditions",
    "cookie_policy", "refund_policy", "crm-signup", "signup-complete",
    "blog:index", "blog:category", "blog:detail",
}

DESCRIPTIONS = {
    "/": "SHVYA AI helps Indian businesses manage leads, automate WhatsApp follow-ups, qualify enquiries with AI and connect sales workflows. Book a demo.",
    "/features/": "Explore SHVYA AI CRM, WhatsApp and Instagram conversations, AI playbooks, sales cadence, workflows and connected lead sources.",
    "/pricing/": "Explore SHVYA AI plans for CRM, WhatsApp follow-ups and AI sales automation. Find the right setup for your team.",
    "/book-a-call/": "Request a SHVYA AI demo to explore lead management, WhatsApp follow-ups, AI qualification and sales workflows for your business.",
}


def marketing_context(request):
    measurement_id = getattr(settings, "GOOGLE_ANALYTICS_ID", "")
    # Values become HTML attributes; require the actual GA4 ID format.
    if not re.fullmatch(r"G-[A-Z0-9]+", measurement_id):
        measurement_id = ""
    route = getattr(getattr(request, "resolver_match", None), "view_name", "")
    # Dedicated CRM/admin cookies are deliberately treated conservatively, even
    # when stale. Do not query customer sessions merely to enable analytics.
    private_visitor = (
        getattr(getattr(request, "user", None), "is_authenticated", False)
        or getattr(request, "crm_user", None)
        or any(request.COOKIES.get(name) for name in (
            "shvya_crm_sessionid", "shvya_superadmin_sessionid",
        ))
    )
    if (route not in PUBLIC_ROUTES or private_visitor or settings.DEBUG
            or request.get_host().split(":")[0] not in {"shvya-ai.com", "www.shvya-ai.com"}):
        measurement_id = ""
    origin = settings.BLOG_PUBLIC_ORIGIN.rstrip("/")
    schema = json.dumps({
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "Organization", "@id": origin + "/#organization", "name": "SHVYA AI", "url": origin + "/"},
            {"@type": "WebSite", "@id": origin + "/#website", "name": "SHVYA AI", "url": origin + "/",
             "publisher": {"@id": origin + "/#organization"}},
        ],
    }).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return {
        "marketing_schema": schema,
        "marketing_canonical": settings.BLOG_PUBLIC_ORIGIN.rstrip("/") + request.path,
        "marketing_description": DESCRIPTIONS.get(request.path, DESCRIPTIONS["/"]),
        "marketing_ga_id": measurement_id,
        "marketing_google_verification": getattr(settings, "GOOGLE_SITE_VERIFICATION", ""),
    }
