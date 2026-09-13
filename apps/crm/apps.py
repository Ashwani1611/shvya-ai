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
        from apps.crm.views import filtering as filtering_views
        from services.crm.dashboard_query_service import (
            build_lead_table_context,
        )

        # Conversation summaries are generated asynchronously. A lead card can
        # therefore be rendered before the summary exists and remain on screen
        # after the summary has been published. Using the rendered boolean as a
        # status indicator makes the card stale even though the summary modal
        # reads the current database state.
        #
        # Treat the card as an action instead: always expose "View summary" and
        # let the modal remain the authoritative live state. If a summary has
        # not been generated yet, the modal already explains that clearly.
        def build_lead_table_context_with_summary_action(*args, **kwargs):
            context = build_lead_table_context(*args, **kwargs)

            for group in context.get("stage_groups", []):
                for lead in group.get("leads", []):
                    lead.has_conversation_summary = True

            return context

        dashboard_views._build_lead_table_context = (
            build_lead_table_context_with_summary_action
        )

        # Standalone lead-card refreshes bypass the lead-table builder, so keep
        # the same action-only state there as well. This prevents a refreshed
        # card from reintroducing the stale "No summary yet" copy.
        original_lead_card_context = dashboard_views._lead_card_context

        def lead_card_context_with_summary_action(lead, user):
            context = original_lead_card_context(lead, user)
            context["conversation_summary"] = True
            return context

        dashboard_views._lead_card_context = (
            lead_card_context_with_summary_action
        )

        # The CRM filter/search endpoint builds lead cards through a separate
        # preparation helper in views/filtering.py. That path bypasses both
        # dashboard hooks above, so filtered or stage-refreshed cards could
        # still render the stale "No summary yet" state. Keep the same summary
        # action contract there as well.
        original_filtered_prepare_lead = filtering_views._prepare_lead

        def filtered_prepare_lead_with_summary_action(
            lead,
            attribute_definitions,
        ):
            prepared_lead = original_filtered_prepare_lead(
                lead,
                attribute_definitions,
            )
            prepared_lead.has_conversation_summary = True
            return prepared_lead

        filtering_views._prepare_lead = (
            filtered_prepare_lead_with_summary_action
        )
