"""Deterministic 0-10 lead intent score for sales visibility.

This is internal sales intelligence, not qualification authority. It uses only
persisted inbound conversation evidence plus backend qualification state and
never calls an AI provider, changes stages, or sends customer messages.
"""
from __future__ import annotations

import re
from copy import deepcopy

from django.utils import timezone

from apps.ai_engagement.services.qualification_state import QUALIFICATION_STATE_KEY


INTENT_SCORE_STATE_KEY = "_shvya_ai_intent_score"
INTENT_SCORE_VERSION = 1

_SOCIAL_ONLY = {
    "hi", "hii", "hello", "hey", "ok", "okay", "thanks", "thank you",
    "cool", "great", "sure", "yes", "no", "y", "n",
}

_STRONG_URGENCY_RE = re.compile(
    r"\b(?:asap|immediately|right away|today|tomorrow|this week|"
    r"within\s+(?:[1-7]|one|two|three|four|five|six|seven)\s+days?|"
    r"within\s+(?:a|one)\s+week)\b",
    re.I,
)
_MEDIUM_URGENCY_RE = re.compile(
    r"\b(?:within\s+(?:30|thirty)\s+days?|within\s+(?:a|one)\s+month|"
    r"this month|next month)\b",
    re.I,
)
_LIGHT_URGENCY_RE = re.compile(
    r"\b(?:next quarter|within\s+[23]\s+months?|just exploring|exploring|later|soon)\b",
    re.I,
)
_STRONG_COMMITMENT_RE = re.compile(
    r"\b(?:ready to (?:buy|purchase|proceed|start)|want to proceed|"
    r"book|schedule)\b.{0,50}\b(?:demo|call|meeting|appointment)\b|"
    r"\b(?:call me|arrange a call|set up a demo)\b",
    re.I,
)
_SOFT_COMMITMENT_RE = re.compile(
    r"\b(?:interested|pricing|price|cost|budget|send (?:me )?(?:details|brochure|proposal)|"
    r"share (?:the )?(?:details|pricing)|decision maker|purchase decision)\b",
    re.I,
)
_CLARITY_RE = re.compile(
    r"\b(?:need|want|goal|problem|challenge|issue|manage|automate|follow[- ]?up|"
    r"leads?|crm|whatsapp|sales|conversion|tracking)\b",
    re.I,
)


def _normalized(value) -> str:
    return " ".join(str(value or "").strip().casefold().split()).strip(" .!?;:,")


def _latest_inbound_texts(lead, *, limit=40) -> list[str]:
    from apps.channels.models import WhatsAppMessage

    rows = (
        WhatsAppMessage.objects.filter(
            organization_id=lead.organization_id,
            lead=lead,
            direction=WhatsAppMessage.Direction.INBOUND,
        )
        .exclude(body="")
        .order_by("-created_at", "-id")
        .values_list("body", flat=True)[:limit]
    )
    return [str(value or "").strip() for value in reversed(list(rows)) if str(value or "").strip()]


def _qualification_facts(lead) -> list[dict[str, str]]:
    """Return backend-normalized answered requirements for scoring context."""
    attrs = lead.attributes if isinstance(getattr(lead, "attributes", None), dict) else {}
    state = attrs.get(QUALIFICATION_STATE_KEY)
    if not isinstance(state, dict):
        return []

    states = state.get("requirement_states")
    states = states if isinstance(states, dict) else {}
    snapshot = state.get("flow_snapshot")
    snapshot = snapshot if isinstance(snapshot, list) else []
    requirement_by_id = {
        str(item.get("id") or ""): item
        for item in snapshot
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }

    facts = []
    for requirement_id, answer_state in states.items():
        if (
            not isinstance(answer_state, dict)
            or str(answer_state.get("status") or "").casefold() != "answered"
        ):
            continue
        value = answer_state.get("value")
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        requirement = requirement_by_id.get(str(requirement_id), {})
        question = str(
            requirement.get("label")
            or str(requirement.get("question") or "").splitlines()[0]
            or requirement_id
        ).strip()
        facts.append({"question": question, "value": str(value).strip()})
    return facts


def _qualification_answer_count(lead) -> int:
    return len(_qualification_facts(lead))


def _engagement_points(texts: list[str]) -> tuple[int, str]:
    meaningful = [
        text for text in texts
        if _normalized(text) not in _SOCIAL_ONLY and len(_normalized(text)) > 2
    ]
    if not meaningful:
        return 0, "Only greeting/acknowledgement-level engagement so far."
    rich = any(len(text) >= 80 or "?" in text for text in meaningful)
    if len(meaningful) >= 4 or (len(meaningful) >= 3 and rich):
        return 3, "Multiple meaningful turns with active detail/questions."
    if len(meaningful) >= 2 or rich:
        return 2, "More than one useful reply or meaningful detail was shared."
    return 1, "One meaningful lead response is available."


def _urgency_points(
    texts: list[str],
    qualification_facts: list[dict[str, str]] | None = None,
) -> tuple[int, str]:
    fact_text = " ".join(
        f"{item.get('question', '')} {item.get('value', '')}"
        for item in (qualification_facts or [])
    )
    joined = " ".join([*texts[-20:], fact_text])
    if _STRONG_URGENCY_RE.search(joined):
        return 3, "Explicit immediate/this-week timeline."
    if _MEDIUM_URGENCY_RE.search(joined):
        return 2, "Explicit implementation timeline within about 30 days."
    if _LIGHT_URGENCY_RE.search(joined):
        return 1, "Longer or exploratory timeline."
    return 0, "No explicit timeline yet."


def _clarity_points(lead, texts: list[str]) -> tuple[int, str]:
    answered = _qualification_answer_count(lead)
    specific = sum(
        1 for text in texts[-20:]
        if len(_normalized(text)) >= 12 and _CLARITY_RE.search(text)
    )
    if answered >= 3 or specific >= 2:
        return 2, "Need/use case is supported by multiple concrete details."
    if answered >= 1 or specific >= 1:
        return 1, "A basic need or use case is clear."
    return 0, "Need is still vague or not stated."


def _commitment_points(
    texts: list[str],
    qualification_facts: list[dict[str, str]] | None = None,
) -> tuple[int, str]:
    joined = " ".join(texts[-20:])
    if _STRONG_COMMITMENT_RE.search(joined):
        return 2, "Firm next-step signal such as a call/demo/proceed request."

    facts = qualification_facts or []
    supplied_budget = any(
        "budget" in _normalized(item.get("question"))
        and bool(_normalized(item.get("value")))
        for item in facts
    )
    if supplied_budget:
        return 1, "Lead supplied a budget in the configured qualification flow."
    if _SOFT_COMMITMENT_RE.search(joined):
        return 1, "Soft buying signal such as budget/pricing/interest."
    return 0, "No explicit commitment signal yet."


def compute_intent_score(*, lead) -> dict:
    texts = _latest_inbound_texts(lead)
    qualification_facts = _qualification_facts(lead)
    engagement, engagement_reason = _engagement_points(texts)
    urgency, urgency_reason = _urgency_points(texts, qualification_facts)
    clarity, clarity_reason = _clarity_points(lead, texts)
    commitment, commitment_reason = _commitment_points(texts, qualification_facts)
    score = engagement + urgency + clarity + commitment
    return {
        "version": INTENT_SCORE_VERSION,
        "score": int(max(0, min(10, score))),
        "max_score": 10,
        "components": {
            "engagement": {
                "label": "Engagement",
                "score": engagement,
                "max": 3,
                "reason": engagement_reason,
            },
            "urgency": {
                "label": "Urgency / Timeline",
                "score": urgency,
                "max": 3,
                "reason": urgency_reason,
            },
            "clarity": {
                "label": "Clarity of Need",
                "score": clarity,
                "max": 2,
                "reason": clarity_reason,
            },
            "commitment": {
                "label": "Commitment Signal",
                "score": commitment,
                "max": 2,
                "reason": commitment_reason,
            },
        },
        "authority": "deterministic_conversation_evidence",
        "updated_at": timezone.now().isoformat(),
    }


def stored_intent_score(*, lead) -> dict | None:
    attrs = lead.attributes if isinstance(getattr(lead, "attributes", None), dict) else {}
    state = attrs.get(INTENT_SCORE_STATE_KEY)
    if not isinstance(state, dict):
        return None
    score = state.get("score")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 10:
        return None
    components = state.get("components")
    if not isinstance(components, dict):
        return None
    return deepcopy(state)


def intent_score_for_lead(*, lead) -> dict:
    return stored_intent_score(lead=lead) or compute_intent_score(lead=lead)


def persist_intent_score(*, lead) -> dict:
    score = compute_intent_score(lead=lead)
    attrs = deepcopy(lead.attributes) if isinstance(getattr(lead, "attributes", None), dict) else {}
    attrs[INTENT_SCORE_STATE_KEY] = deepcopy(score)
    lead.__class__.objects.filter(pk=lead.pk).update(attributes=attrs)
    lead.attributes = attrs
    return score
