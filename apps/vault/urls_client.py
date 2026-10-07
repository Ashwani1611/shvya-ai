from django.urls import path
from . import views_client as views

urlpatterns = [
    path("files/<uuid:entry_id>/", views.signed_file, name="vault-signed-file"),
    path("<slug:slug>/", views.vault_client, name="vault-client"),
    path("<slug:slug>/export.md", views.vault_export, name="vault-client-export"),
    path("<slug:slug>/files/<uuid:entry_id>/", views.vault_file, name="vault-client-file"),
]
