from django.urls import path

from . import template_cta_ui
from .template_cta_public import tracked_template_cta


urlpatterns = [
    # These dashboard paths intentionally precede the broad channels include in
    # config.urls. Both views retain normal CRM authentication and tenant checks.
    path(
        "dashboard/whatsapp/templates/<uuid:template_id>/analytics/",
        template_cta_ui.template_analytics,
        name="whatsapp-template-analytics-tracking",
    ),
    path(
        "dashboard/whatsapp/templates/<uuid:template_id>/enable-click-tracking/",
        template_cta_ui.enable_template_cta_tracking,
        name="whatsapp-template-enable-click-tracking",
    ),
    path(
        "w/cta/<str:token>/<path:suffix>/",
        tracked_template_cta,
        name="whatsapp-template-cta-action-dynamic",
    ),
    path(
        "w/cta/<str:token>/",
        tracked_template_cta,
        name="whatsapp-template-cta-action",
    ),
]
