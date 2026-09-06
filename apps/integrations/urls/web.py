from django.urls import path
from django.views.generic import RedirectView

from apps.integrations.views.web import (
    connect_hub_view,
    integration_detail_view,
    shvya_api_view,
)
from apps.integrations.views.webhook import webhook_view


urlpatterns = [
    path(
        "connect-hub/",
        connect_hub_view,
        name="crm-connect-hub",
    ),
    path(
        "connect-hub/shvya-api/",
        shvya_api_view,
        name="crm-connect-hub-shvya-api",
    ),
    path(
        "connect-hub/webhook/",
        webhook_view,
        name="crm-connect-hub-webhook",
    ),
    path(
        "connect-hub/google-sheets/",
        integration_detail_view,
        {"integration_slug": "google-sheets"},
        name="crm-connect-hub-google-sheets",
    ),
    path(
        "connect-hub/email/",
        integration_detail_view,
        {"integration_slug": "email"},
        name="crm-connect-hub-email",
    ),
    path(
        "connect-hub/meta-conversions-api/",
        integration_detail_view,
        {"integration_slug": "meta-conversions-api"},
        name="crm-connect-hub-meta-conversions-api",
    ),
    path(
        "connect-hub/meta-lead-ad-forms/",
        integration_detail_view,
        {"integration_slug": "meta-lead-ad-forms"},
        name="crm-connect-hub-meta-lead-ad-forms",
    ),
    path(
        "connect-hub/razorpay/",
        integration_detail_view,
        {"integration_slug": "razorpay"},
        name="crm-connect-hub-razorpay",
    ),
    path(
        "connect-hub/justdial/",
        integration_detail_view,
        {"integration_slug": "justdial"},
        name="crm-connect-hub-justdial",
    ),
    path(
        "connect-hub/indiamart/",
        integration_detail_view,
        {"integration_slug": "indiamart"},
        name="crm-connect-hub-indiamart",
    ),
    path(
        "integrations-hub/",
        RedirectView.as_view(
            pattern_name="crm-connect-hub",
            permanent=True,
        ),
        name="crm-integrations-hub",
    ),
]
