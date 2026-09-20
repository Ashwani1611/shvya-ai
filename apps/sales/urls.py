from django.urls import path

from apps.sales import views


urlpatterns = [
    path("", views.sales_dashboard_view, name="shvya-sales-dashboard"),
    path("documents/", views.sales_document_list_view, name="shvya-sales-document-list"),
    path(
        "documents/new/<str:document_type>/",
        views.sales_document_create_view,
        name="shvya-sales-document-create",
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
    path("templates/", views.sales_template_list_view, name="shvya-sales-template-list"),
    path("templates/new/", views.sales_template_form_view, name="shvya-sales-template-create"),
    path(
        "templates/<uuid:template_id>/edit/",
        views.sales_template_form_view,
        name="shvya-sales-template-edit",
    ),
]
