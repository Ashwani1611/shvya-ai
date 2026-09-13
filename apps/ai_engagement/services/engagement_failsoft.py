"""Deterministic last-resort customer reply when model validation cannot recover.

The normal engagement path remains authoritative and is always attempted first.
This module exists only to prevent a genuine production WhatsApp turn from
ending in silence because both the primary model result and its schema-repair
result failed validation. It never invents CRM actions, qualification answers,
business facts, pipeline movement, attributes, reminders, or identifiers.
"""

from __future__ import annotations

import logging
import re


_INSTALLED = False
logger = logging.getLogger(__name__)

_SIMPLE_ACKS = {
    "ok", "okay", "thanks", "thank you", "sure", "fine", "great", "done",
    "got it", "understood",
}
_HUMAN_TERMS = (
    "call me", "call back", "callback", "speak with", "talk to", "discuss with",
    "human", "agent", "representative", "support", "demo", "meeting", "session",
)
_BOOKING_TERMS = ("booked", "scheduled", "appointment", "demo", "meeting", "session")
_PLAN_TERMS = ("plan", "plans", "pack", "packs", "package", "packages", "price", "pricing", "cost", "fees")
_CAPABILITY_TERMS = ("feature", "features", "service", "services", "capability", "capabilities", "automate", "automation")
_FRUSTRATION_TERMS = (
    "same thing", "same message", "repeating", "repeat", "100 bar", "chup",
    "bc", "mc", "fucker", "fuck", "randi",
)
_STOP_WORDS = {
    "a", "an", "and", "are", "can", "could", "do", "does", "for", "from",
    "how", "i", "in", "is", "it", "me", "my", "of", "on", "or", "please",
    "the", "this", "to", "want", "what", "where", "who", "with", "you", "your",
}


def _clean(value) -> str:
    return " ".join(str(value or "").strip().split())


def _normalized(value) -> str:
    return _clean(value).casefold()


def _latest_inbound_for_lead(*, organization, lead):
    return (
        lead.whatsapp_messages.filter(
            organization_id=organization.pk,
            direction="inbound",
        )
        .order_by("-created_at", "-id")
        .first()
    )


def _fact_lines(about: str) -> list[str]:
    """Return authored fact lines without section labels or bullet markers."""
    result: list[str] = []
    for raw in str(about or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        # Section labels are useful for grouping but are not customer-facing facts.
        if line.endswith(":") and len(line) <= 90:
            continue
        line = re.sub(r"^[-*•]\s*", "", line).strip()
        if line and line not in result:
            result.append(line)
    return result


def _section_items(about: str, terms: tuple[str, ...]) -> list[str]:
    """Extract authored bullets underneath a matching short section heading."""
    lines = str(about or "").splitlines()
    collecting = False
    result: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            if collecting and result:
                break
            continue
        is_heading = line.endswith(":") and len(line) <= 90
        if is_heading:
            heading = line[:-1].strip().casefold()
            if collecting:
                break
            collecting = any(term in heading for term in terms)
            continue
        if collecting:
            cleaned = re.sub(r"^[-*•]\s*", "", line).strip()
            if cleaned:
                result.append(cleaned)
    return result


def _tokenize(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", _normalized(value))
        if len(token) > 2 and token not in _STOP_WORDS
    }


def _best_authored_facts(*, about: str, inbound: str, organization_name: str = "") -> list[str]:
    lines = _fact_lines(about)
    if not lines:
        return []

    normalized = _normalized(inbound)
    query_tokens = _tokenize(inbound) - _tokenize(organization_name)

    # Explicit lead-generation questions should surface both the business-model
    # positive and any authored limitation, when present.
    if "lead" in normalized and ("generat" in normalized or "lead generation" in normalized):
        matched = [
            line for line in lines
            if "lead" in _normalized(line)
            and any(term in _normalized(line) for term in ("generat", "sell leads", "existing leads", "convert"))
        ]
        if matched:
            return matched[:2]

    scored: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        tokens = _tokenize(line)
        score = len(query_tokens & tokens)
        normalized_line = _normalized(line)
        if any(term in normalized for term in _CAPABILITY_TERMS) and any(
            term in normalized_line for term in ("lead", "crm", "follow", "whatsapp", "sales", "automation", "response", "qualification")
        ):
            score += 2
        if score:
            scored.append((score, -index, line))
    scored.sort(reverse=True)
    return [item[2] for item in scored[:2]]


def _grounded_conversation_reply(*, about: str, inbound: str, organization_name: str) -> tuple[str, str]:
    """Return a provider-free reply containing only authored facts or safe process language."""
    text = _clean(inbound)
    normalized = text.casefold()

    if normalized in _SIMPLE_ACKS:
        return "Sure.", "NORMAL_CONVERSATION"

    if any(term in normalized for term in _FRUSTRATION_TERMS):
        return "Understood. I’ll keep it concise and avoid repeating myself.", "NORMAL_CONVERSATION"

    booking_claim = any(term in normalized for term in _BOOKING_TERMS) and any(
        token in normalized for token in ("i booked", "i have booked", "already booked", "i scheduled", "already scheduled", "no one called")
    )
    if booking_claim:
        if "no one called" in normalized or "didn't call" in normalized or "did not call" in normalized:
            return (
                "I’m sorry you haven’t received the call. I can’t confirm or trigger a callback from here; the team would need to check the booking.",
                "HUMAN_HANDOFF",
            )
        return (
            "Got it. I’ll treat that as a booking you’ve reported, but I can’t confirm the appointment unless the booking system confirms it.",
            "NORMAL_CONVERSATION",
        )

    if any(term in normalized for term in _HUMAN_TERMS):
        return (
            "I understand you want to speak with someone. I can’t confirm a callback or meeting from here; it needs to be confirmed by the team or booking system.",
            "HUMAN_HANDOFF",
        )

    if any(term in normalized for term in _CAPABILITY_TERMS):
        items = _section_items(about, ("capabil", "feature", "service"))
        if items:
            return "Key capabilities include: " + "; ".join(items[:6]) + ".", "ANSWER_ORG_QUESTION"

    facts = _best_authored_facts(
        about=about,
        inbound=text,
        organization_name=organization_name,
    )
    if facts:
        return " ".join(facts), "ANSWER_ORG_QUESTION"

    overview_intent = (
        normalized.startswith("what is ")
        or normalized.startswith("tell me about ")
        or (organization_name and _normalized(organization_name) in normalized)
    )
    if overview_intent:
        lines = _fact_lines(about)
        if lines:
            return lines[0], "ANSWER_ORG_QUESTION"

    if any(term in normalized for term in _PLAN_TERMS):
        return (
            "I don’t have confirmed plan or pricing details in the information available to me. The team would need to confirm them.",
            "UNKNOWN_INFORMATION",
        )

    return (
        "I don’t have enough verified information to answer that confidently. The team would need to confirm it.",
        "UNKNOWN_INFORMATION",
    )


def build_deterministic_fallback_decision(*, organization, lead, latest_inbound=None):
    """Build a provider-free, RAG-free response from persisted backend state.

    ``latest_inbound`` lets provider-specific workers bind runtime intent to the
    exact conversation they own. Generic callers may omit it and keep the
    existing lead-wide behavior.
    """
    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.engagement import EngagementDecision
    from apps.ai_engagement.services.organization_profile import (
        compile_qualification_requirements,
    )
    from apps.ai_engagement.services.qualification_state import (
        MODE_QUALIFICATION,
        next_requirement,
        requirements_for_lead,
        state_for_lead,
    )
    from apps.ai_engagement.services.runtime_state import STATE_KEY, observe_message

    org_info = (
        OrgInfo.objects.filter(organization_id=organization.pk)
        .only("qualification_requirements", "about")
        .first()
    )
    compiled = compile_qualification_requirements(
        org_info.qualification_requirements if org_info else ""
    )
    requirements = requirements_for_lead(
        lead,
        compiled.get("requirements", []),
    )
    state = state_for_lead(lead, requirements=requirements)

    attributes = lead.attributes if isinstance(getattr(lead, "attributes", None), dict) else {}
    runtime = attributes.get(STATE_KEY, {})
    if latest_inbound is None:
        latest_inbound = _latest_inbound_for_lead(organization=organization, lead=lead)
    latest_text = str(getattr(latest_inbound, "body", "") or "").strip() if latest_inbound is not None else ""
    if latest_inbound is not None:
        runtime = observe_message(runtime, latest_text)

    # Explicit pause/opt-out remains authoritative even if generation failed.
    conversation_mode = runtime.get("conversation_mode")
    if conversation_mode in {"paused", "opt_out"}:
        reason = "OPT_OUT" if conversation_mode == "opt_out" else "NO_ACTION"
        return EngagementDecision(
            should_engage=False,
            message="",
            file_document_id=None,
            crm_actions=[],
            reason=reason,
            reason_code=reason,
            model="deterministic-fallback",
        )

    about = org_info.about if org_info else ""
    organization_name = str(getattr(organization, "name", "") or "")

    # An information question, booking statement or human request interrupts
    # qualification for this turn. Answer that intent safely and leave the
    # backend qualification state untouched so it can resume on re-engagement.
    normalized = _normalized(latest_text)
    interrupt = (
        "?" in latest_text
        or any(term in normalized for term in _HUMAN_TERMS)
        or any(term in normalized for term in _PLAN_TERMS)
        or any(term in normalized for term in _CAPABILITY_TERMS)
        or normalized.startswith("what is ")
        or "lead generation" in normalized
        or "generate leads" in normalized
        or "booked" in normalized
        or "scheduled" in normalized
        or "no one called" in normalized
    )
    if interrupt:
        message, reason = _grounded_conversation_reply(
            about=about,
            inbound=latest_text,
            organization_name=organization_name,
        )
        return EngagementDecision(
            should_engage=True,
            message=message,
            file_document_id=None,
            crm_actions=[],
            reason=reason,
            reason_code=reason,
            model="deterministic-fallback",
        )

    item = next_requirement(requirements, state.get("requirement_states", {}))
    if (
        state.get("engagement_mode") == MODE_QUALIFICATION
        and state.get("qualification_status") != "completed"
        and isinstance(item, dict)
        and str(item.get("question") or "").strip()
    ):
        # Use the exact backend-compiled question. It already contains authored
        # options in order and does not reconstruct or invent questionnaire text.
        return EngagementDecision(
            should_engage=True,
            message=str(item["question"]).strip(),
            file_document_id=None,
            crm_actions=[],
            reason="QUALIFICATION_NEXT",
            reason_code="QUALIFICATION_NEXT",
            next_requirement_id=str(item.get("id") or "") or None,
            model="deterministic-fallback",
        )

    message, reason = _grounded_conversation_reply(
        about=about,
        inbound=latest_text,
        organization_name=organization_name,
    )
    return EngagementDecision(
        should_engage=True,
        message=message,
        file_document_id=None,
        crm_actions=[],
        reason=reason,
        reason_code=reason,
        model="deterministic-fallback",
    )


def _fallback_decision(*, service, organization, lead):
    """Compatibility wrapper for the installed EngagementService guard."""
    latest_inbound = None
    resolver = getattr(service.context_builder, "latest_inbound_for_fallback", None)
    if callable(resolver):
        latest_inbound = resolver(organization=organization, lead=lead)
    return build_deterministic_fallback_decision(
        organization=organization,
        lead=lead,
        latest_inbound=latest_inbound,
    )


def install_engagement_failsoft() -> None:
    """Wrap the production provider path with a safe last-resort reply.

    Tests and explicit service callers frequently inject a provider specifically
    to validate strict schema/security failures. Those calls must continue to
    raise EngagementError. Production workers instantiate EngagementService
    without an injected provider, so only that path receives fail-soft behavior.
    The worker also owns a second terminal guard so this wrapper is defense in
    depth rather than the only protection against customer-facing silence.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import engagement as engagement_module
    from apps.ai_engagement.services.ai_provider import AIProviderTransientError

    service_class = engagement_module.EngagementService
    original_engage = service_class.engage

    def engage(self, *, organization, lead, knowledge_query=None, context=None):
        try:
            return original_engage(
                self,
                organization=organization,
                lead=lead,
                knowledge_query=knowledge_query,
                context=context,
            )
        except engagement_module.EngagementError as exc:
            # Explicit/injected providers are used by callers that need strict
            # validation semantics. Do not convert their failures into replies.
            if self.provider is not None:
                raise
            # Celery remains the single retry owner for temporary provider/network
            # faults. Fail-soft is for permanent provider/schema/validation cases.
            if isinstance(exc.__cause__, AIProviderTransientError):
                raise
            logger.exception(
                "AI engagement validation/provider path failed for lead %s; using deterministic fail-soft reply",
                getattr(lead, "pk", None),
            )
            return _fallback_decision(
                service=self,
                organization=organization,
                lead=lead,
            )

    service_class.engage = engage
    _INSTALLED = True
