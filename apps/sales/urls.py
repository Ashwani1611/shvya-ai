from django.urls import path

from apps.sales import lifecycle_views, views
from apps.sales.preview import template_preview


urlpatterns = [
    path("templates/preview/", template_preview, name="shvya-sales-template-preview"),
    path("", views.sales_dashboard_view, name="shvya-sales-dashboard"),
    path("settings/", lifecycle_views.sales_settings_view, name="shvya-sales-settings"),
    path("documents/", views.sales_document_list_view, name="shvya-sales-document-list"),
    path(
        "documents/new/<str:document_type>/",
        views.sales_document_create_view,
        name="shvya-sales-document-create",
    ),
    path(
        "documents/<uuid:document_id>/edit/",
        views.sales_document_edit_view,
        name="shvya-sales-document-edit",
    ),
    path(
        "documents/<uuid:document_id>/",
        views.sales_document_detail_view,
        name="shvya-sales-document-detail",
    ),
    path(
        "documents/<uuid:document_id>/send/",
        views.sales_document_send_view,
        name="shvya-sales-document-send",
    ),
    path(
        "documents/<uuid:document_id>/pdf/",
        lifecycle_views.sales_document_pdf_view,
        name="shvya-sales-document-pdf",
    ),
    path(
        "documents/<uuid:document_id>/attachments/add/",
        lifecycle_views.sales_attachment_add_view,
        name="shvya-sales-attachment-add",
    ),
    path(
        "attachments/<uuid:attachment_id>/download/",
        lifecycle_views.sales_attachment_download_view,
        name="shvya-sales-attachment-download",
    ),
    path(
        "attachments/<uuid:attachment_id>/delete/",
        lifecycle_views.sales_attachment_delete_view,
        name="shvya-sales-attachment-delete",
    ),
    path(
        "documents/<uuid:document_id>/agreement-revision/",
        lifecycle_views.sales_agreement_revision_view,
        name="shvya-sales-agreement-revision",
    ),
    path(
        "documents/<uuid:document_id>/payments/record/",
        lifecycle_views.sales_manual_payment_view,
        name="shvya-sales-manual-payment",
    ),
    path(
        "documents/<uuid:document_id>/refunds/record/",
        lifecycle_views.sales_manual_refund_view,
        name="shvya-sales-manual-refund",
    ),
    path(
        "payments/<uuid:payment_id>/refund/",
        lifecycle_views.sales_gateway_refund_view,
        name="shvya-sales-gateway-refund",
    ),
    path(
        "documents/<uuid:document_id>/credits/create/",
        lifecycle_views.sales_credit_note_create_view,
        name="shvya-sales-credit-create",
    ),
    path(
        "credits/<uuid:credit_id>/apply/",
        lifecycle_views.sales_credit_note_apply_view,
        name="shvya-sales-credit-apply",
    ),
    path(
        "documents/<uuid:document_id>/recurring/",
        lifecycle_views.sales_recurring_invoice_view,
        name="shvya-sales-recurring",
    ),
    path(
        "documents/<uuid:document_id>/payment-link/",
        lifecycle_views.sales_payment_link_create_view,
        name="shvya-sales-payment-link",
    ),
    path(
        "scheduled/<uuid:schedule_id>/cancel/",
        lifecycle_views.sales_schedule_cancel_view,
        name="shvya-sales-schedule-cancel",
    ),
    path("templates/", views.sales_template_list_view, name="shvya-sales-template-list"),
    path("templates/new/", views.sales_template_form_view, name="shvya-sales-template-create"),
    path(
        "templates/<uuid:template_id>/edit/",
        views.sales_template_form_view,
        name="shvya-sales-template-edit",
    ),
]
