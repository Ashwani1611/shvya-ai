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
INTENT_SCORE_VERSION = 2

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
    r"\b(?:next quarter|within\s+[23]\s+months?|later|soon)\b",
    re.I,
)
_LONG_TERM_OR_BROWSING_RE = re.compile(
    r"\b(?:just exploring|exploring|just browsing|browsing|"
    r"more than\s+3\s+months?|after\s+3\s+months?|"
    r"(?:4|5|6|7|8|9|10|11|12)\s+months?)\b",
    re.I,
)
_STRONG_COMMITMENT_RE = re.compile(
    r"\b(?:ready to (?:buy|purchase|proceed|start)|want to proceed|"
    r"book|schedule)\b.{0,50}\b(?:demo|call|meeting|appointment)\b|"
    r"\b(?:call me|arrange a call|set up a demo|demo booked|meeting booked|"
    r"budget approved|approved budget|decision[- ]?maker|final purchase decision)\b",
    re.I,
)
_SOFT_COMMITMENT_RE = re.compile(
    r"\b(?:maybe later|follow[- ]?up|contact me|check back|let me think|"
    r"get back to me|reach out later|interested)\b",
    re.I,
)
_CLARITY_RE = re.compile(
    r"\b(?:need|want|goal|problem|challenge|issue|manage|automate|follow[- ]?up|"
    r"leads?|crm|whatsapp|sales|conversion|tracking)\b",
    re.I,
)


def _normalized(value) -> str:
    return " ".join(str(value or "").strip().casefold().split()).strip(" .!?;:,")


def prepare_intent_scores(leads):
    """Two bounded evidence queries per tenant, never one query per card."""
    from collections import defaultdict
    from django.db.models import F, Window
    from django.db.models.functions import RowNumber
    from apps.channels.models import WhatsAppMessage
    from apps.channels.instagram_models import InstagramMessage

    groups = defaultdict(list)
    for lead in leads:
        groups[lead.organization_id].append(lead)
    for org_id, group in groups.items():
        evidence = defaultdict(list)
        ids = [lead.pk for lead in group]
        for model, relation in ((WhatsAppMessage, "lead_id"), (InstagramMessage, "conversation__lead_id")):
            filters = {"organization_id": org_id, "account__organization_id": org_id, relation + "__in": ids, "direction": "inbound"}
            if model is InstagramMessage:
                filters.update(conversation__organization_id=org_id, account__organization_id=org_id)
            rows = model.objects.filter(**filters).exclude(body="").annotate(
                evidence_rank=Window(RowNumber(), partition_by=[F(relation)],
                                     order_by=[F("created_at").desc(), F("pk").desc()])
            ).filter(evidence_rank__lte=40).values_list(relation, "created_at", "pk", "body")
            for lead_id, at, pk, body in rows:
                if body.strip():
                    evidence[lead_id].append((at, str(pk), body.strip()))
        for lead in group:
            lead._intent_texts = [row[2] for row in sorted(evidence[lead.pk])[-40:]]
            lead.intent_score_state = compute_intent_score(lead=lead)


def _latest_inbound_texts(lead, *, limit=40) -> list[str]:
    if not hasattr(lead, "_intent_texts"):
        prepare_intent_scores([lead])
    return lead._intent_texts[-limit:]


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
        return 0, "Only one-word or generic replies so far."

    joined = " ".join(meaningful[-20:])
    rich_detail = any(len(text) >= 80 for text in meaningful)
    actively_driving = bool(re.search(
        r"\b(?:book|schedule|request|want|need|show me|explain)\b.{0,40}"
        r"\b(?:demo|call|meeting|pricing|details)\b",
        joined,
        re.I,
    ))
    if rich_detail or actively_driving or len(meaningful) >= 4:
        return 3, "Lead volunteered rich information or actively drove the conversation."
    if any("?" in text for text in meaningful) or len(meaningful) >= 2:
        return 2, "Lead asked clarifying questions or supplied useful detail."
    return 1, "Lead answered questions but offered little extra detail."


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
        return 3, "Needs action this week or ASAP."
    if _MEDIUM_URGENCY_RE.search(joined):
        return 2, "Wants a solution within one month."
    if _LONG_TERM_OR_BROWSING_RE.search(joined):
        return 0, "Timeline is beyond three months or the lead is only browsing."
    if _LIGHT_URGENCY_RE.search(joined):
        return 1, "Timeline is roughly one to three months or otherwise vague."
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
    facts = qualification_facts or []
    fact_text = " ".join(
        f"{item.get('question', '')} {item.get('value', '')}"
        for item in facts
    )
    combined = f"{joined} {fact_text}"

    if _STRONG_COMMITMENT_RE.search(combined):
        return 2, "Firm next step, approved budget, or decision-maker involvement."

    supplied_budget = any(
        "budget" in _normalized(item.get("question"))
        and bool(_normalized(item.get("value")))
        for item in facts
    )
    if supplied_budget or _SOFT_COMMITMENT_RE.search(joined):
        return 1, "Soft commitment or follow-up interest."
    return 0, "No next-step commitment was provided."


def _qualification_answer_ratio(lead) -> tuple[int, int, float]:
    attrs = lead.attributes if isinstance(getattr(lead, "attributes", None), dict) else {}
    state = attrs.get(QUALIFICATION_STATE_KEY)
    if not isinstance(state, dict):
        return 0, 0, 0.0
    snapshot = state.get("flow_snapshot")
    snapshot = snapshot if isinstance(snapshot, list) else []
    required_ids = [
        str(item.get("id") or "")
        for item in snapshot
        if isinstance(item, dict)
        and item.get("required", True)
        and str(item.get("id") or "").strip()
    ]
    states = state.get("requirement_states")
    states = states if isinstance(states, dict) else {}
    answered = sum(
        1
        for requirement_id in required_ids
        if str((states.get(requirement_id) or {}).get("status") or "").casefold()
        == "answered"
    )
    total = len(required_ids)
    ratio = answered / total if total else 0.0
    return answered, total, ratio


def compute_intent_score(*, lead) -> dict:
    texts = _latest_inbound_texts(lead)
    qualification_facts = _qualification_facts(lead)
    engagement, engagement_reason = _engagement_points(texts)
    urgency, urgency_reason = _urgency_points(texts, qualification_facts)
    clarity, clarity_reason = _clarity_points(lead, texts)
    commitment, commitment_reason = _commitment_points(texts, qualification_facts)
    raw_score = engagement + urgency + clarity + commitment
    answered, required, answer_ratio = _qualification_answer_ratio(lead)
    eighty_percent_override = bool(required) and answer_ratio >= 0.8
    score = max(raw_score, 8) if eighty_percent_override else raw_score
    return {
        "version": INTENT_SCORE_VERSION,
        "score": int(max(0, min(10, score))) if texts or qualification_facts else None,
        "assessed": bool(texts or qualification_facts),
        "evidence_count": len(texts),
        "raw_score": int(max(0, min(10, raw_score))),
        "max_score": 10,
        "qualified_threshold": 8,
        "meets_qualified_threshold": score >= 8,
        "eighty_percent_override": eighty_percent_override,
        "questions_answered": answered,
        "questions_required": required,
        "answered_ratio": round(answer_ratio, 4),
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
    if not isinstance(state, dict) or state.get("version") != INTENT_SCORE_VERSION:
        return None
    score = state.get("score")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 10:
        return None
    components = state.get("components")
    if not isinstance(components, dict):
        return None
    return deepcopy(state)


def intent_score_for_lead(*, lead) -> dict:
    return compute_intent_score(lead=lead)


def persist_intent_score(*, lead) -> dict:
    from django.db import transaction
    with transaction.atomic():
        current = lead.__class__.objects.select_for_update().get(
            pk=lead.pk, organization_id=lead.organization_id,
        )
        score = compute_intent_score(lead=current)
        attrs = deepcopy(current.attributes) if isinstance(current.attributes, dict) else {}
        attrs[INTENT_SCORE_STATE_KEY] = deepcopy(score)
        lead.__class__.objects.filter(pk=current.pk, organization_id=current.organization_id).update(attributes=attrs)
    lead.attributes = attrs
    return score
