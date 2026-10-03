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
    organization = context.organization if isinstance(context.organization, dict) else {}
    stage = context.stage if isinstance(context.stage, dict) else {}
    state = qualification_state if isinstance(qualification_state, dict) else {}

    stage_name = normalize_stage_name(stage.get("name"))
    qualification_mode = (
        state.get("engagement_mode") == MODE_QUALIFICATION
        or stage_name == "new lead"
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
    needs_knowledge = bool(context.knowledge) or service._should_retrieve_knowledge(
        context=context
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
            "when approved facts are available, acknowledge any newly captured answer "
            "naturally, then ask at most the single backend-selected next requirement. "
            "Never restart or reorder the questionnaire."
        )
    return (
        "TURN MODE: SALES SUPPORT. Do not restart New Lead qualification. Help with "
        "the customer's current sales/support intent using the current stage, approved "
        "organization facts and retrieved knowledge. Propose only backend-allowed actions."
    )
