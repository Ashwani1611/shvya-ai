"""Make current CRM location unambiguous while keeping routing metadata internal."""

from __future__ import annotations


_INSTALLED = False


def install_pipeline_context_guard() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services.context import AIContextBuilder

    original = AIContextBuilder._build_pipeline_context

    def build(builder, *, lead):
        context = original(builder, lead=lead)
        if not isinstance(context, dict):
            return context

        current = {
            "id": str(lead.pipeline_id) if lead.pipeline_id else None,
            "name": getattr(getattr(lead, "pipeline", None), "name", ""),
        }
        guarded = {
            **context,
            "current_pipeline": current,
            "routing_metadata_internal_only": True,
        }

        available_pipelines = []
        for item in guarded.get("available_pipelines") or []:
            if isinstance(item, dict):
                available_pipelines.append({**item, "customer_visible": False})
            else:
                available_pipelines.append(item)
        if available_pipelines:
            guarded["available_pipelines"] = available_pipelines

        available_stages = []
        for item in guarded.get("available_stages") or []:
            if isinstance(item, dict):
                available_stages.append({**item, "customer_visible": False})
            else:
                available_stages.append(item)
        if available_stages:
            guarded["available_stages"] = available_stages

        return guarded

    AIContextBuilder._build_pipeline_context = build
    _INSTALLED = True
