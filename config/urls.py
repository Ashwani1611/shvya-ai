from django.contrib import admin
from django.templatetags.static import static
from django.urls import include, path
from django.views.generic import RedirectView, TemplateView

from apps.channels.instagram_webhook import instagram_webhook_view
from apps.channels.webhook_security import whatsapp_webhook_secure_view
from apps.core.health import live as health_live, ready as health_ready
from apps.core.runtime_status import runtime_metrics
from apps.integrations.views.meta_leads import meta_lead_webhook
from apps.superadmin.views import admin_global_search

from apps.core.views import BookCallView, DocumentationView, FeaturesView, HomeView, PricingView


urlpatterns = [
    # Remote read-only SHVYA diagnostic MCP + OAuth discovery.
    path("", include("apps.integrations.urls.diagnostics")),
    path("health/live/", health_live, name="health-live"),
    path("health/ready/", health_ready, name="health-ready"),
    path("health/runtime-metrics/", runtime_metrics, name="runtime-metrics"),
    path("sales/", include("apps.sales.public_urls")),
    path("features/", FeaturesView.as_view(), name="features"),
    path('dashboard/workflows/', include('apps.triggers.urls.web')),
    # Dedicated support routes must precede the broad dashboard/admin includes.
    path("dashboard/support-portal/", include("apps.support.urls_customer")),
    path("superadmin/client-portal/", include("apps.support.urls_staff")),
    path("support/", include("apps.support.urls_shared")),
    # =========================================================
    # Browser favicon
    # =========================================================
    path(
        "favicon.ico",
        RedirectView.as_view(
            url=static("images/shvya-brand-2026.png"),
            permanent=False,
        ),
        name="favicon",
    ),

    # =========================================================
    # SHVYA Admin — Global Search
    # =========================================================
    path(
        "admin/search/",
        admin_global_search,
        name="admin-global-search",
    ),

    path(
        "",
        HomeView.as_view(),
        name="home",
    ),

    # =========================================================
    # Django Admin
    # =========================================================
    path(
        "admin/",
        admin.site.urls,
    ),

    # =========================================================
    # Versioned API
    #
    # Keep one canonical composition point under api/v1/urls.py while
    # preserving every externally visible route.
    # =========================================================
    path("api/v1/", include("api.v1.urls")),

    # =========================================================
    # Accounts
    # =========================================================
    path(
        "",
        include("apps.accounts.urls"),
    ),

    # =========================================================
    # Sales Desk Web Dashboard
    # =========================================================
    path(
        "dashboard/sales-desk/",
        include("apps.copilot.urls.web"),
    ),

    # =========================================================
    # Cadence Web Dashboard
    #
    # Keep this before the broad CRM dashboard include so the real feature
    # owns /dashboard/cadence/* rather than a legacy placeholder.
    # =========================================================
    path(
        "dashboard/cadence/",
        include("apps.followups.urls.web"),
    ),

    # =========================================================
    # SHVYA Sales Web Dashboard
    # =========================================================
    path(
        "dashboard/sales/",
        include("apps.sales.urls"),
    ),

    # =========================================================
    # SHVYA Calendar
    # =========================================================
    path(
        "dashboard/shvya-calendar/",
        include("apps.shvya_calendar.urls"),
    ),
    path(
        "calendar/",
        include("apps.shvya_calendar.public_urls"),
    ),

    # =========================================================
    # Connect Hub Web Dashboard
    #
    # Keep this before the broad CRM dashboard include so the integrations
    # app owns /dashboard/connect-hub/* and the legacy redirect.
    # =========================================================
    path(
        "dashboard/",
        include("apps.integrations.urls.web"),
    ),

    # =========================================================
    # Apple Stage Editor
    #
    # Keep this before the broad CRM include so the dedicated modal endpoints
    # own /dashboard/stage-editor/* without disturbing legacy stage routes.
    # =========================================================
    path(
        "dashboard/stage-editor/",
        include("apps.crm.urls.stage_editor"),
    ),

    # =========================================================
    # Call Intelligence — Android SIM calling
    # =========================================================
    path("dashboard/call-intelligence/", include("apps.telephony.urls.web")),

    # =========================================================
    # CRM Web Dashboard
    # =========================================================
    path(
        "dashboard/",
        include("apps.crm.urls.web"),
    ),

    # =========================================================
    # WhatsApp Channels
    # =========================================================
    path(
        "dashboard/whatsapp/",
        include("apps.channels.urls"),
    ),

    # =========================================================
    # Teams
    # =========================================================
    path(
        "dashboard/teams/",
        include("apps.teams.urls.web"),
    ),

    # =========================================================
    # Insights
    # =========================================================
    path(
        "dashboard/insights/",
        include("apps.analytics.urls.web"),
    ),

    # =========================================================
    # WhatsApp Webhook
    # =========================================================
    path(
        "webhooks/whatsapp/",
        whatsapp_webhook_secure_view,
        name="whatsapp-webhook",
    ),
    path(
        "webhooks/instagram/",
        instagram_webhook_view,
        name="instagram-webhook",
    ),

    path(
        "webhooks/meta-leads/",
        meta_lead_webhook,
        name="meta-lead-webhook",
    ),

    # =========================================================
    # Super Admin Console
    # =========================================================
    path(
        "superadmin/",
        include("apps.superadmin.urls"),
    ),

    # =========================================================
    # Marketing Pages
    # =========================================================
    path(
        "pricing/",
        PricingView.as_view(),
        name="pricing",
    ),
    path(
        "docs/",
        DocumentationView.as_view(),
        name="docs",
    ),
    path(
        "book-a-call/",
        BookCallView.as_view(),
        name="book_call",
    ),
    path(
        "book/",
        RedirectView.as_view(pattern_name="book_call", permanent=False),
        name="book",
    ),
    path(
        "services/",
        TemplateView.as_view(template_name="services.html"),
        name="services",
    ),
    path(
        "product-suite/",
        TemplateView.as_view(template_name="product_suite.html"),
        name="product_suite",
    ),
    path(
        "privacy-policy/",
        TemplateView.as_view(template_name="legal/privacy_policy.html"),
        name="privacy_policy",
    ),
    path(
        "terms-conditions/",
        TemplateView.as_view(template_name="legal/terms_conditions.html"),
        name="terms_conditions",
    ),
    path(
        "cookie-policy/",
        TemplateView.as_view(template_name="legal/cookie_policy.html"),
        name="cookie_policy",
    ),
    path(
        "refund-policy/",
        TemplateView.as_view(template_name="legal/refund_policy.html"),
        name="refund_policy",
    ),

]
