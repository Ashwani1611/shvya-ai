"""Public marketing metadata and optional Google measurement configuration."""
import json
import re
from django.conf import settings

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
