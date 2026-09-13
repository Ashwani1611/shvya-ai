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
        from apps.ai_engagement.models import InternalConversationSummary
        from apps.crm.views import dashboard as dashboard_views
        from services.crm.dashboard_query_service import (
            build_lead_table_context,
        )

        dashboard_views._build_lead_table_context = (
            build_lead_table_context
        )

        # The standalone lead-card refresh path does not use the lead-table
        # builder, so preserve the original context builder and enrich only
        # the Conversation Summary state. This keeps every existing card
        # behavior unchanged while preventing a refreshed card from falling
        # back to "No summary yet" when an active summary already exists.
        original_lead_card_context = dashboard_views._lead_card_context

        def lead_card_context_with_summary(lead, user):
            context = original_lead_card_context(lead, user)
            context["conversation_summary"] = (
                InternalConversationSummary.objects
                .filter(
                    organization=user.organization,
                    lead=lead,
                    is_active=True,
                )
                .exclude(summary="")
                .exists()
            )
            return context

        dashboard_views._lead_card_context = lead_card_context_with_summary
