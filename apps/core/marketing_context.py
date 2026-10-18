"""Public marketing metadata and optional Google measurement configuration."""
import json
import re
from urllib.parse import urljoin

from django.conf import settings
from django.templatetags.static import static

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
    "/services/": "Explore SHVYA website and application development services, from workflow discovery and CRM integrations to testing, launch and support.",
    "/product-suite/": "Explore the SHVYA product suite for sales, CRM, inventory, billing, HR and business operations. Find the tools that fit your team's workflow.",
    "/docs/": "Browse SHVYA AI documentation for CRM, WhatsApp, AI automation, integrations, sales and troubleshooting. Start with practical setup guides.",
    "/cookie-policy/": "Learn how SHVYA AI uses essential cookies and optional analytics, how consent works, and how to manage your cookie preferences.",
    "/refund-policy/": "Read SHVYA AI's refund policy, including eligibility, cancellation conditions, request requirements and the refund review process.",
    "/privacy-policy/": "Read how SHVYA AI collects, uses and protects information, including account data, communications, analytics and your privacy choices.",
    "/terms-conditions/": "Review the terms for using SHVYA AI, including account responsibilities, service use, payments and applicable conditions.",
}

TITLES = {
    "/": "SHVYA AI | CRM & WhatsApp Sales Automation for Indian Businesses",
    "/features/": "CRM, WhatsApp & AI Sales Automation Features | SHVYA AI",
    "/pricing/": "CRM & AI Sales Automation Pricing | SHVYA AI",
    "/services/": "Website & Application Development Services | SHVYA AI",
    "/product-suite/": "CRM & Business Software Product Suite | SHVYA AI",
    "/docs/": "Documentation & Setup Guides | SHVYA AI",
    "/book-a-call/": "Book a CRM & AI Sales Automation Demo | SHVYA AI",
    "/cookie-policy/": "Cookie Policy | SHVYA AI",
    "/refund-policy/": "Refund Policy | SHVYA AI",
    "/privacy-policy/": "Privacy Policy | SHVYA AI",
    "/terms-conditions/": "Terms & Conditions | SHVYA AI",
}


def public_page_metadata(path, *, title=None, description=None, breadcrumbs=()):
    """Build page metadata from public content, never request hosts or queries."""
    origin = settings.BLOG_PUBLIC_ORIGIN.rstrip("/")
    canonical = origin + path
    title = title or TITLES.get(path, "SHVYA AI")
    description = description or DESCRIPTIONS.get(path, DESCRIPTIONS["/"])
    image = urljoin(origin + "/", static("images/shvya-brand-2026.png"))
    graph = [
        {"@type": "Organization", "@id": origin + "/#organization", "name": "SHVYA AI", "url": origin + "/", "logo": image},
        {"@type": "WebSite", "@id": origin + "/#website", "name": "SHVYA AI", "url": origin + "/",
         "publisher": {"@id": origin + "/#organization"}},
        {"@type": "WebPage", "@id": canonical + "#webpage", "url": canonical,
         "name": title, "description": description, "isPartOf": {"@id": origin + "/#website"}},
    ]
    if breadcrumbs:
        graph.append({"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": position, "name": name, "item": origin + item_path}
            for position, (name, item_path) in enumerate(breadcrumbs, 1)
        ]})
    schema = json.dumps({"@context": "https://schema.org", "@graph": graph})
    return {
        "marketing_schema": schema.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"),
        "marketing_canonical": canonical,
        "marketing_title": title,
        "marketing_description": description,
        "marketing_image": image,
        "marketing_robots": "index, follow, max-image-preview:large",
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
    return {
        **public_page_metadata(request.path),
        "marketing_ga_id": measurement_id,
        "marketing_google_verification": getattr(settings, "GOOGLE_SITE_VERIFICATION", ""),
    }
