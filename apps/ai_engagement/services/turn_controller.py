from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from apps.ai_engagement.services.qualification_state import (
    MODE_QUALIFICATION,
    next_requirement,
    normalize_stage_name,
)


QUALIFICATION_MODE = "qualification"
SALES_SUPPORT_MODE = "sales_support"
MAX_OPERATING_SPEC_CHARS = 50000
DEFAULT_KNOWLEDGE_LIMIT = 5


@dataclass(frozen=True)
class TurnPolicy:
    """One explicit, transport-neutral policy for a customer AI turn."""

    prompt_mode: str
    model_override: str
    operating_spec: str
    knowledge_limit: int = DEFAULT_KNOWLEDGE_LIMIT

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_mode": self.prompt_mode,
            "model_override": self.model_override,
            "operating_spec": self.operating_spec,
            "knowledge_limit": self.knowledge_limit,
        }


def build_turn_policy(*, context, qualification_state: dict | None = None) -> TurnPolicy:
    raw_organization = getattr(context, "organization", {})
    raw_stage = getattr(context, "stage", {})
    organization = raw_organization if isinstance(raw_organization, dict) else {}
    stage = raw_stage if isinstance(raw_stage, dict) else {}
    state = qualification_state if isinstance(qualification_state, dict) else {}

    stage_name = normalize_stage_name(stage.get("name"))
    # Stage is the primary prompt-mode authority: New Lead/New Leads runs the
    # qualification conversation; every other concrete stage runs sales support.
    # State is used only when a synthetic/preview context has no real stage.
    qualification_mode = (
        stage_name == "new lead"
        if stage_name
        else state.get("engagement_mode") == MODE_QUALIFICATION
    )
    prompt_mode = QUALIFICATION_MODE if qualification_mode else SALES_SUPPORT_MODE
    model_key = "qualification_model" if qualification_mode else "sales_support_model"

    return TurnPolicy(
        prompt_mode=prompt_mode,
        model_override=str(organization.get(model_key) or "").strip()[:100],
        operating_spec=str(organization.get("ai_playbook") or "").strip()[
            :MAX_OPERATING_SPEC_CHARS
        ],
        knowledge_limit=DEFAULT_KNOWLEDGE_LIMIT,
    )


def build_business_plan(
    *,
    service,
    context,
    qualification_state: dict,
    requirements: list[dict],
    latest_text: str,
    turn_policy: TurnPolicy,
) -> dict[str, Any]:
    """Build the deterministic plan the response model must express."""

    next_item = next_requirement(
        requirements,
        (qualification_state or {}).get("requirement_states", {}),
    )
    retrieve_check = getattr(service, "_should_retrieve_knowledge", None)
    needs_knowledge = bool(getattr(context, "knowledge", None)) or (
        bool(retrieve_check(context=context)) if callable(retrieve_check) else False
    )
    qualifying = turn_policy.prompt_mode == QUALIFICATION_MODE
    priority = (
        "answer_then_qualify"
        if needs_knowledge and qualifying and next_item
        else "answer_customer"
        if needs_knowledge
        else "continue_qualification"
        if qualifying and next_item
        else "sales_support"
    )

    return {
        "prompt_mode": turn_policy.prompt_mode,
        "priority": priority,
        "answer_customer_first": bool(needs_knowledge),
        "next_requirement_id": (
            str(next_item.get("id") or "") if isinstance(next_item, dict) else None
        ),
        "next_requirement_question": (
            str(next_item.get("question") or "").strip()
            if isinstance(next_item, dict)
            else ""
        ),
        "knowledge_available": bool(context.knowledge),
        "knowledge_count": len(context.knowledge or []),
        "latest_customer_message": str(latest_text or "")[:2000],
        "action_authority": "backend_only",
        "qualification_authority": "backend_only",
    }


def record_turn_policy(*, policy: TurnPolicy, business_plan: dict | None = None) -> None:
    try:
        from apps.ai_engagement.services.trace_service import record

        payload = {
            "prompt_mode": policy.prompt_mode,
            "model_override_configured": bool(policy.model_override),
            "knowledge_limit": policy.knowledge_limit,
        }
        if isinstance(business_plan, dict):
            payload["business_plan"] = {
                key: business_plan.get(key)
                for key in (
                    "priority",
                    "answer_customer_first",
                    "next_requirement_id",
                    "knowledge_available",
                    "knowledge_count",
                )
            }
        record("turn", payload)
    except Exception:
        return


def prompt_mode_instructions(policy: TurnPolicy) -> str:
    if policy.prompt_mode == QUALIFICATION_MODE:
        return (
            "TURN MODE: QUALIFICATION. Answer the customer's actual question first "
            "using AI Brain and the organization's AI Playbook, acknowledge any newly captured answer "
            "naturally, then ask at most the single backend-selected next requirement. "
            "Never restart or reorder the questionnaire."
        )
    return (
        "TURN MODE: SALES SUPPORT. Do not restart New Lead qualification. Help with "
        "the customer's current sales/support intent using AI Brain, AI Playbook and "
        "the current stage. Propose only backend-allowed actions."
    )


class TurnController:
    """Canonical entry point for customer-facing AI decisions across channels.

    Channel adapters supply only their scoped context builder/provider. All
    conversation reasoning still runs through the same EngagementService and
    LangGraph runtime, so API, Hosted, Instagram and Sandbox share one decision
    contract while keeping transport delivery separate.
    """

    def __init__(self, *, provider=None, context_builder=None, service_class=None) -> None:
        self.provider = provider
        self.context_builder = context_builder
        self.service_class = service_class

    def service(self):
        if self.service_class is None:
            from apps.ai_engagement.services.engagement import EngagementService
            service_class = EngagementService
        else:
            service_class = self.service_class

        kwargs = {}
        if self.provider is not None:
            kwargs["provider"] = self.provider
        if self.context_builder is not None:
            kwargs["context_builder"] = self.context_builder
        return service_class(**kwargs)

    def engage(self, *, organization, lead, knowledge_query=None, context=None):
        return self.service().engage(
            organization=organization,
            lead=lead,
            knowledge_query=knowledge_query,
            context=context,
        )
