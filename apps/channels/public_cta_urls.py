from django.urls import path

from services.channels.template_cta_meta_compat import (
    tracked_template_cta_compatible,
)

from . import template_cta_ui


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
    # Meta requires a dynamic URL variable to be the final URL component, so
    # this route intentionally has no literal slash after ``suffix``.
    path(
        "w/cta/<str:token>/<path:suffix>",
        tracked_template_cta_compatible,
        name="whatsapp-template-cta-action-dynamic",
    ),
    path(
        "w/cta/<str:token>/",
        tracked_template_cta_compatible,
        name="whatsapp-template-cta-action",
    ),
]
