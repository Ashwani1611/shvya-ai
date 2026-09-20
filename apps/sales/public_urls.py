from django.urls import path

from apps.sales import views


urlpatterns = [
    path("<uuid:token>/", views.public_document_view, name="shvya-sales-public-document"),
    path(
        "<uuid:token>/action/",
        views.public_document_action_view,
        name="shvya-sales-public-action",
    ),
]
