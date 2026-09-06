from django.apps import AppConfig


class CrmConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.crm"
    label = "crm"

    def ready(self):
        from .models import signals  # noqa: F401

        # Keep the public dashboard view API stable while moving the heavy
        # lead-table query construction into a focused service. This avoids a
        # risky rewrite of the large dashboard view module and lets every
        # existing HTMX endpoint use the optimized builder automatically.
        from apps.crm.views import dashboard as dashboard_views
        from services.crm.dashboard_query_service import (
            build_lead_table_context,
        )

        dashboard_views._build_lead_table_context = (
            build_lead_table_context
        )
