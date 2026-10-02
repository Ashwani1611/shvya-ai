from django.urls import path

from .template_cta_public import tracked_template_cta


urlpatterns = [
    path(
        "w/cta/<str:token>/",
        tracked_template_cta,
        name="whatsapp-template-cta-action",
    ),
]
