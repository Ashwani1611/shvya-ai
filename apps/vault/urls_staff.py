from django.urls import path
from . import views_staff as views

urlpatterns = [
    path("", views.vault_list, name="vault-staff-list"),
    path("<uuid:vault_id>/", views.vault_detail, name="vault-staff-detail"),
    path("<uuid:vault_id>/export.md", views.vault_export, name="vault-staff-export"),
    path("<uuid:vault_id>/files/<uuid:entry_id>/", views.vault_file, name="vault-staff-file"),
    path("<uuid:vault_id>/profiles/<uuid:snapshot_id>/", views.vault_profile, name="vault-staff-profile"),
]
