from django.urls import path

from apps.sales import lifecycle_views, views


urlpatterns = [
    path(
        "track/open/<uuid:tracking_token>.gif",
        lifecycle_views.sales_email_open_view,
        name="shvya-sales-email-open",
    ),
    path(
        "track/click/<uuid:token>/",
        lifecycle_views.sales_email_click_view,
        name="shvya-sales-email-click",
    ),
    path(
        "email/provider-event/",
        lifecycle_views.sales_email_provider_event_view,
        name="shvya-sales-email-provider-event",
    ),
    path(
        "payments/webhook/<uuid:gateway_id>/",
        lifecycle_views.sales_payment_webhook_view,
        name="shvya-sales-payment-webhook",
    ),
    path(
        "<uuid:token>/pdf/",
        lifecycle_views.sales_public_pdf_view,
        name="shvya-sales-public-pdf",
    ),
    path(
        "<uuid:token>/attachments/<uuid:attachment_id>/",
        lifecycle_views.sales_public_attachment_view,
        name="shvya-sales-public-attachment",
    ),
    path("<uuid:token>/", views.public_document_view, name="shvya-sales-public-document"),
    path(
        "<uuid:token>/action/",
        views.public_document_action_view,
        name="shvya-sales-public-action",
    ),
]
