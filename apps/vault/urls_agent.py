from django.urls import path
from . import api

app_name = "vault_agent"
urlpatterns = [
    path("workspace", api.workspace, name="workspace"),
    path("export.md", api.export, name="export"),
    path("entries", api.entries, name="entries"),
    path("questions", api.questions, name="questions"),
    path("calls", api.calls, name="calls"),
]
