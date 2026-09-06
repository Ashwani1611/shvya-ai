from django.contrib import admin
from django.templatetags.static import static
from django.urls import include, path
from django.views.generic import RedirectView, TemplateView

from apps.channels import views_flat as channels_views_flat
from apps.superadmin.views import admin_global_search

from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

from apps.core.views import HomeView, PricingView


urlpatterns = [
    path('dashboard/smart-triggers/', include('apps.triggers.urls.web')),
    # =========================================================
    # Browser favicon
    #
    # Keep this at the root so every HTML page on the domain gets
    # the SHVYA icon even when that page does not declare an explicit
    # <link rel="icon"> tag.
    # =========================================================
    path(
        "favicon.ico",
        RedirectView.as_view(
            url=static("images/shvya-logo.svg"),
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
    # JWT Authentication
    # =========================================================
    path(
        "api/v1/auth/token/",
        TokenObtainPairView.as_view(),
        name="token_obtain_pair",
    ),
    path(
        "api/v1/auth/token/refresh/",
        TokenRefreshView.as_view(),
        name="token_refresh",
    ),

    # =========================================================
    # CRM API
    # =========================================================
    path(
        "api/v1/leads/",
        include("apps.crm.urls.api_v1"),
    ),

    # =========================================================
    # Co-Pilot API
    # =========================================================
    path(
        "api/v1/copilot/",
        include("apps.copilot.urls.api_v1"),
    ),

    # =========================================================
    # Teams API
    # =========================================================
    path(
        "api/v1/teams/",
        include("apps.teams.urls.api_v1"),
    ),

    # =========================================================
    # Accounts
    # =========================================================
    path(
        "",
        include("apps.accounts.urls"),
    ),

    # =========================================================
    # Co-Pilot Web Dashboard
    # =========================================================
    path(
        "dashboard/copilot/",
        include("apps.copilot.urls.web"),
    ),

    # =========================================================
    # Auto Follow-ups Web Dashboard
    #
    # Keep this before the broad CRM dashboard include so the real feature
    # owns /dashboard/auto-follow-ups/* rather than a legacy placeholder.
    # =========================================================
    path(
        "dashboard/auto-follow-ups/",
        include("apps.followups.urls.web"),
    ),

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
    # Analytics
    # =========================================================
    path(
        "dashboard/analytics/",
        include("apps.analytics.urls.web"),
    ),

    # =========================================================
    # WhatsApp Webhook
    # =========================================================
    path(
        "webhooks/whatsapp/",
        channels_views_flat.whatsapp_webhook_view,
        name="whatsapp-webhook",
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

    # =========================================================
    # AI ENGAGEMENT API
    # =========================================================
    path(
        "api/v1/ai-engagement/",
        include("apps.ai_engagement.urls.api_v1"),
    ),
]
