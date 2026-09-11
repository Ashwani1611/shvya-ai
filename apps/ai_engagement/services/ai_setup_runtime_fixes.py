from __future__ import annotations

import json
import re
from typing import Any


_INSTALLED = False

_FILE_KNOWLEDGE_TERMS = {
    "brochure", "catalog", "catalogue", "pdf", "file", "document", "deck",
    "presentation", "menu", "prospectus", "portfolio", "flyer", "leaflet",
    "datasheet", "sheet", "price", "pricing", "pricelist", "rates",
}

_INTERRUPT_TERMS = {
    "booked a call", "book a call", "schedule a call", "call me", "call back",
    "speak with", "talk to", "human", "agent", "support", "demo booked",
}

_ADDITIONAL_ENGAGEMENT_INSTRUCTIONS = r"""
BACKEND QUALIFICATION AUTHORITY
- The backend qualification state is the only authority for sequence, lifecycle,
  completion, and the active requirement.
- Never reconstruct a questionnaire from conversation history or Organization
  Qualification Requirements. Never choose an earlier/later requirement yourself.
- CURRENT_REQUIREMENT/NEXT_REQUIREMENT supplied by the application is the only
  qualification question that may be presented on this turn.
- ANSWERED, SKIPPED and NOT_APPLICABLE requirements are immutable for sequencing
  and must never be asked again unless the backend explicitly reopens them.
- A/B/C/D, numeric option aliases, yes/no, and short replies apply only to the
  backend's active requirement. Do not search the questionnaire for another match.
- When the active requirement was already resolved by backend state for this
  inbound message, do not emit another qualification_update for that message.
- If qualification_status is completed, do not restart qualification. Send a
  short acknowledgment when appropriate and continue normal conversation.

PIPELINE STAGE TRANSITIONS
- pipeline.available_stages is the complete allow-list for stage movement.
- For non-Qualified destinations, use each destination stage description as the
  movement criterion. Propose at most one pipeline_transition only when current
  conversation evidence clearly satisfies that destination description.
- Never invent a stage id and never move a lead merely from vague positivity.
- The Qualified destination remains application-controlled and may be selected
  only when deterministic qualification evaluation permits it.
""".strip()


def _clean_spaces(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _enhanced_evaluate_qualification(original_evaluator):
    def evaluate(*, runtime_policy: dict[str, Any], projected_state: dict[str, Any]):
        result = original_evaluator(
            runtime_policy=runtime_policy,
            projected_state=projected_state,
        )
        mode = str(
            ((runtime_policy.get("qualification") or {}).get("mode") or "configured")
        ).strip().casefold()
        if mode != "majority":
            return result

        required_results = [
            item for item in result.get("criteria", []) if item.get("required", True)
        ]
        if not required_results:
            return {**result, "outcome": "not_configured"}
        unresolved = [item for item in required_results if item.get("verdict") == "unknown"]
        if unresolved:
            return {**result, "outcome": "in_progress"}
        passes = sum(1 for item in required_results if item.get("verdict") == "pass")
        needed = (len(required_results) // 2) + 1
        return {**result, "outcome": "qualified" if passes >= needed else "not_qualified"}

    return evaluate


def _enhanced_controlled_actions(original_builder):
    def build(
        *,
        decision,
        context,
        runtime_policy: dict[str, Any],
        qualification_state: dict[str, Any],
        requirements: list[dict[str, Any]],
    ):
        controlled, result = original_builder(
            decision=decision,
            context=context,
            runtime_policy=runtime_policy,
            qualification_state=qualification_state,
            requirements=requirements,
        )

        if any(item.get("type") == "pipeline_transition" for item in controlled):
            return controlled, result

        pipeline = context.pipeline if isinstance(context.pipeline, dict) else {}
        available_stages = pipeline.get("available_stages") or []
        current_stage = context.stage if isinstance(context.stage, dict) else {}
        current_stage_id = str(current_stage.get("id") or "")
        by_id = {
            str(item.get("id")): item
            for item in available_stages
            if isinstance(item, dict) and item.get("id") is not None
        }

        for action in getattr(decision, "crm_actions", []) or []:
            if not isinstance(action, dict) or action.get("type") != "pipeline_transition":
                continue
            stage_shift = action.get("stage_shift")
            if not isinstance(stage_shift, dict):
                continue
            stage_id = str(stage_shift.get("stage_id") or "").strip()
            destination = by_id.get(stage_id)
            if not stage_id or destination is None or stage_id == current_stage_id:
                continue
            destination_name = _clean_spaces(destination.get("name")).casefold()
            if destination_name == "qualified":
                continue
            description = _clean_spaces(destination.get("description"))
            if not description:
                continue
            controlled.append({
                "type": "pipeline_transition",
                "stage_shift": {"stage_id": stage_id},
            })
            result = {
                **result,
                "stage_transition": {"stage_id": stage_id, "source": "stage_description"},
            }
            break
        return controlled, result

    return build


def _enhanced_should_retrieve_knowledge(original_method):
    def should_retrieve(self, *, context):
        text = self._latest_inbound_text(context=context)
        normalized = _clean_spaces(text).casefold()
        words = set(re.findall(r"[a-z0-9]+", normalized))
        if words & (set(self._KNOWLEDGE_TERMS) | _FILE_KNOWLEDGE_TERMS):
            return True
        return original_method(self, context=context)

    return should_retrieve


def _normalize_question_for_repeat_check(value: str) -> str:
    first_line = str(value or "").splitlines()[0]
    return re.sub(r"[^a-z0-9]+", " ", first_line.casefold()).strip()


def _message_contains_question(message: str, requirement: dict | None) -> bool:
    if not requirement:
        return False
    needle = _normalize_question_for_repeat_check(
        str(requirement.get("question") or requirement.get("label") or "")
    )
    haystack = re.sub(r"[^a-z0-9]+", " ", str(message or "").casefold()).strip()
    return bool(needle and needle in haystack)


def _enhanced_qualification_validator(original_validator, engagement_module):
    def validate(self, *, decision, context, requirements, qualification_state):
        original_validator(
            self,
            decision=decision,
            context=context,
            requirements=requirements,
            qualification_state=qualification_state,
        )

        from apps.ai_engagement.services.qualification_state import (
            REQUIREMENT_ANSWERED,
            next_requirement,
            project_answer_updates,
        )

        projected = project_answer_updates(
            state=qualification_state,
            requirements=requirements,
            updates=getattr(decision, "qualification_updates", []) or [],
            messages=(context.conversation or {}).get("messages", []),
        )
        next_item = next_requirement(requirements, projected.get("requirement_states", {}))
        if projected.get("qualification_status") == "completed":
            next_item = None
        next_id = str(next_item.get("id") or "") if next_item else ""

        reason_code = str(getattr(decision, "reason_code", "") or "").strip().upper()
        selected_next = str(getattr(decision, "next_requirement_id", "") or "").strip()
        if selected_next and selected_next != next_id:
            raise engagement_module.EngagementError(
                "Qualification response may use only the backend-selected next requirement."
            )
        if reason_code in {"QUALIFICATION_NEXT", "QUALIFICATION_CLARIFY"}:
            if not next_id:
                raise engagement_module.EngagementError(
                    "Qualification is complete. Acknowledge completion and do not ask another qualification question."
                )
            if selected_next != next_id:
                raise engagement_module.EngagementError(
                    "Qualification response must ask only the backend-selected next requirement."
                )

        latest_id = self._latest_inbound_message_id(context=context)
        latest_text = self._latest_inbound_text(context=context)
        message = str(getattr(decision, "message", "") or "").strip()
        last_id = str(qualification_state.get("last_asked_requirement_id") or "").strip()
        last_state = (projected.get("requirement_states") or {}).get(last_id, {})
        answered_this_turn = bool(
            last_id
            and last_state.get("status") == REQUIREMENT_ANSWERED
            and str(last_state.get("source_message_id") or "") == str(latest_id or "")
        )
        if answered_this_turn:
            last_requirement = next(
                (item for item in requirements or [] if str(item.get("id") or "") == last_id),
                None,
            )
            if _message_contains_question(message, last_requirement):
                raise engagement_module.EngagementError(
                    "Do not repeat a qualification question that the lead just answered."
                )

        active_id = str(qualification_state.get("current_requirement_id") or last_id or "").strip()
        active_requirement = next(
            (item for item in requirements or [] if str(item.get("id") or "") == active_id),
            None,
        )
        update_ids = {
            str(item.get("requirement_id") or "")
            for item in (getattr(decision, "qualification_updates", []) or [])
            if isinstance(item, dict)
        }
        interrupt = "?" in str(latest_text or "") or any(
            term in _clean_spaces(latest_text).casefold() for term in _INTERRUPT_TERMS
        )
        if (
            interrupt
            and active_id
            and active_id not in update_ids
            and _message_contains_question(message, active_requirement)
        ):
            raise engagement_module.EngagementError(
                "Answer the lead's interruption without repeating the still-pending qualification question in the same response."
            )

        if projected.get("qualification_status") == "completed":
            if "?" not in str(latest_text or "") and "?" in message:
                raise engagement_module.EngagementError(
                    "The final qualification answer is complete. Send an acknowledgment instead of another question."
                )

    return validate


def _backend_authoritative_repair(engagement_module):
    def repair(
        self,
        *,
        provider,
        organization,
        lead,
        result,
        original_error,
        instructions,
        input_text,
        metadata=None,
    ):
        repair_instructions = """
Repair a malformed SHVYA engagement JSON result.
Return ONLY one valid JSON object with exactly these keys:
should_engage, silence_rule, message, file_document_id, crm_actions,
qualification_updates, next_requirement_id, reason_code.
Use the original_turn qualification_turn as the only qualification sequencing
authority. Do NOT reconstruct or recompute the questionnaire from conversation,
organization text, engagement instructions, or history. Do NOT move backward.
If the current inbound was already processed by backend state, do not emit its
qualification update again. Present only the backend-supplied current/following
requirement allowed for this turn. If qualification is complete, acknowledge it
without another qualification question. For an informational/call interruption
that did not answer the active requirement, answer the interruption and leave
next_requirement_id null rather than repeating the pending question.
Drop unsupported evidence/actions. Never invent evidence, identifiers, business
facts, stage ids, or file ids. Do not add explanations or chain-of-thought.
""".strip()
        repair_input = json.dumps(
            {
                "validation_error": str(original_error),
                "malformed_output": result.text,
                "original_turn": json.loads(input_text),
            },
            ensure_ascii=False,
        )
        try:
            repaired = self._generate_provider_text(
                provider=provider,
                instructions=f"{instructions}\n\n{repair_instructions}",
                input_text=repair_input,
                metadata={
                    **(metadata or {
                        "organization_id": str(organization.id),
                        "lead_id": str(lead.id),
                        "task": "engagement",
                    }),
                    "phase": "schema_repair",
                },
                response_schema=engagement_module.ENGAGEMENT_RESPONSE_SCHEMA,
            )
        except engagement_module.AIProviderError as exc:
            raise engagement_module.EngagementError(
                "AI engagement schema repair failed."
            ) from exc
        return self._normalize_result(result=repaired)

    return repair


def install_ai_setup_runtime_fixes() -> None:
    """Install non-sequencing runtime safeguards around the core state engine."""
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.graph import policy_actions as policy_actions_module
    original_evaluator = policy_actions_module.evaluate_qualification
    policy_actions_module.evaluate_qualification = _enhanced_evaluate_qualification(original_evaluator)
    original_action_builder = policy_actions_module.build_controlled_actions
    policy_actions_module.build_controlled_actions = _enhanced_controlled_actions(original_action_builder)

    from apps.ai_engagement.services import engagement as engagement_module
    EngagementService = engagement_module.EngagementService
    EngagementService._KNOWLEDGE_TERMS = set(EngagementService._KNOWLEDGE_TERMS) | _FILE_KNOWLEDGE_TERMS
    original_should_retrieve = EngagementService._should_retrieve_knowledge
    EngagementService._should_retrieve_knowledge = _enhanced_should_retrieve_knowledge(original_should_retrieve)

    original_validator = EngagementService._validate_qualification_decision
    EngagementService._validate_qualification_decision = _enhanced_qualification_validator(
        original_validator,
        engagement_module,
    )
    EngagementService._repair_result_once = _backend_authoritative_repair(engagement_module)

    if _ADDITIONAL_ENGAGEMENT_INSTRUCTIONS not in EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS:
        EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS = (
            f"{EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS}\n\n{_ADDITIONAL_ENGAGEMENT_INSTRUCTIONS}"
        )

    from apps.ai_engagement.prompts import engagement as engagement_prompt_module
    if _ADDITIONAL_ENGAGEMENT_INSTRUCTIONS not in engagement_prompt_module.CUSTOMER_ENGAGEMENT_INSTRUCTIONS:
        engagement_prompt_module.CUSTOMER_ENGAGEMENT_INSTRUCTIONS = (
            f"{engagement_prompt_module.CUSTOMER_ENGAGEMENT_INSTRUCTIONS}\n\n{_ADDITIONAL_ENGAGEMENT_INSTRUCTIONS}"
        )

    _INSTALLED = True
