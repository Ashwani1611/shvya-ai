from __future__ import annotations

import logging


_INSTALLED = False
logger = logging.getLogger(__name__)

_INFORMATION_INTENT_TERMS = (
    "price",
    "pricing",
    "plan",
    "plans",
    "pack",
    "packs",
    "package",
    "packages",
    "cost",
    "fees",
    "feature",
    "features",
    "service",
    "services",
    "product",
    "products",
    "capability",
    "capabilities",
)


def _is_playground_state(state) -> bool:
    context = state.get("context")
    lead_context = getattr(context, "lead", None)
    if isinstance(lead_context, dict):
        source = str(lead_context.get("lead_source") or "").strip().casefold()
        if source == "playground":
            return True

    lead = state.get("lead")
    return str(getattr(lead, "id", "") or "").startswith("playground:")


def _has_interrupting_customer_intent(text: str) -> bool:
    """Return True when qualification must not replace the latest customer intent."""
    raw = str(text or "").strip()
    if not raw:
        return False

    # Reuse the production intent classifier so Sandbox recovery follows the
    # same question/request/problem priority contract as live conversations.
    try:
        from apps.ai_engagement.services.conversation_priority_runtime import (
            _intent_kind,
        )

        if _intent_kind(raw) != "none":
            return True
    except Exception:
        # Recovery must remain available even if the optional runtime patch is
        # not installed yet in a bounded test/import context.
        pass

    normalized = " ".join(raw.casefold().split())
    return any(term in normalized for term in _INFORMATION_INTENT_TERMS)


def install_playground_graph_recovery() -> None:
    """Keep production graph semantics while making Sandbox qualification fail-safe.

    The Sandbox should normally exercise the same provider/validation path as a
    real conversation. If that provider path fails after the backend has already
    accepted a high-confidence qualification answer, the backend-selected next
    question is authoritative and can be returned without another model call.

    Customer questions/requests are different: intent-first routing is
    authoritative. If generation fails on one of those turns, let the outer
    Sandbox grounded fallback answer the latest intent instead of replacing it
    with a pending qualification question.

    This recovery is intentionally scoped to Playground leads. Production
    WhatsApp turns keep the pre-existing deterministic/provider routing contract.
    """

    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.graph import workflow
    from apps.ai_engagement.services.engagement import EngagementDecision
    from apps.ai_engagement.services.qualification_state import (
        MODE_QUALIFICATION,
        REQUIREMENT_ANSWERED,
    )

    original_generate = workflow._generate

    def scoped_deterministic_extract(state):
        if state.get("caller_supplied_context"):
            return {}

        lead = state["lead"]
        requirements = state.get("requirements") or []
        if not requirements:
            return {}

        direct_text = state.get("latest_text", "")
        if _is_playground_state(state):
            # Sandbox users commonly answer a binary option with natural wording
            # such as "YES, I RUN ADS". Restrict that normalization to Sandbox so
            # production answer parsing remains unchanged.
            direct_text = workflow._canonical_yes_no_reply(state, requirements)

        direct = workflow.apply_unambiguous_reply(
            lead=lead,
            requirements=requirements,
            text=direct_text,
            source_message_id=state.get("latest_message_id", ""),
        )
        qualification_state = direct["state"]
        updates = {"qualification_state": qualification_state}

        # Preserve the production direct-route contract that existed before the
        # Sandbox fix. Configured instructions/language/attributes still use the
        # provider in normal operation; only failures are recovered below.
        direct_next = direct.get("next_requirement")
        profile = state.get("profile") or {}
        context = state["context"]
        if (
            not profile.get("communication", {}).get("custom_instructions")
            and not profile.get("qualification", {}).get("raw")
            and not profile.get("communication", {}).get("languages")
            and not (context.pipeline or {}).get("attribute_definitions")
            and direct.get("changed")
            and direct.get("answer_status") == REQUIREMENT_ANSWERED
            and qualification_state.get("engagement_mode") == MODE_QUALIFICATION
            and isinstance(direct_next, dict)
            and direct_next.get("can_direct_ask")
            and str(direct_next.get("question") or "").strip()
        ):
            updates["direct_decision"] = EngagementDecision(
                should_engage=True,
                message=str(direct_next["question"]).strip(),
                file_document_id=None,
                crm_actions=[],
                reason="QUALIFICATION_NEXT",
                reason_code="QUALIFICATION_NEXT",
                next_requirement_id=str(direct_next.get("id") or "") or None,
                model="deterministic",
            )
        return updates

    def playground_safe_generate(state):
        try:
            return original_generate(state)
        except Exception:
            if not _is_playground_state(state):
                raise

            latest_text = str(state.get("latest_text") or "").strip()
            if _has_interrupting_customer_intent(latest_text):
                logger.warning(
                    "ai_sandbox_generation_intent_fallback organization=%s lead=%s",
                    getattr(state.get("organization"), "id", ""),
                    getattr(state.get("lead"), "id", ""),
                    exc_info=True,
                )
                # PlaygroundService owns the grounded last-resort conversation
                # fallback. Re-raising here prevents this graph-only qualification
                # recovery from masking a pricing/product/question intent with Q1.
                raise

            qualification_state = state.get("qualification_state") or {}
            requirements = state.get("requirements") or []
            next_id = str(
                qualification_state.get("next_requirement_id")
                or qualification_state.get("current_requirement_id")
                or ""
            ).strip()
            next_requirement = next(
                (
                    item
                    for item in requirements
                    if str(item.get("id") or "").strip() == next_id
                ),
                None,
            )

            if (
                qualification_state.get("engagement_mode") == MODE_QUALIFICATION
                and qualification_state.get("qualification_status") != "completed"
                and isinstance(next_requirement, dict)
                and str(next_requirement.get("question") or "").strip()
            ):
                question = str(next_requirement["question"]).strip()
                logger.warning(
                    "ai_sandbox_generation_recovered organization=%s lead=%s next_requirement=%s",
                    getattr(state.get("organization"), "id", ""),
                    getattr(state.get("lead"), "id", ""),
                    next_id,
                    exc_info=True,
                )
                return {
                    "decision": EngagementDecision(
                        should_engage=True,
                        message=f"Nice. {question}",
                        file_document_id=None,
                        crm_actions=[],
                        reason="QUALIFICATION_NEXT",
                        reason_code="QUALIFICATION_NEXT",
                        next_requirement_id=next_id or None,
                        model="deterministic-recovery",
                    )
                }
            raise

    workflow._deterministic_extract = scoped_deterministic_extract
    workflow._generate = playground_safe_generate
    # The compiled graph stores node callables. Rebuild once so the scoped
    # functions above are the ones invoked for subsequent engagement turns.
    workflow.ENGAGEMENT_GRAPH = workflow.build_engagement_graph()

    _INSTALLED = True
