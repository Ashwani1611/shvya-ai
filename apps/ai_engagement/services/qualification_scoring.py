from __future__ import annotations

import re
from copy import deepcopy

from django.utils import timezone


LEAD_SCORE_KEY = "_shvya_ai_lead_score"
SCORE_VERSION = 1
QUALIFIED_SCORE = 8

_GENERIC_REPLIES = {
    "hi", "hello", "hey", "ok", "okay", "yes", "no", "sure", "thanks",
    "thank you", "cool", "fine", "done", "interested",
}

_ASAP_RE = re.compile(
    r"\b(?:asap|immediately|right away|today|this week|within\s+7\s+days?|"
    r"next\s+7\s+days?|urgent|urgently)\b",
    re.I,
)
_WITHIN_MONTH_RE = re.compile(
    r"\b(?:within\s+(?:30|[12]?\d)\s+days?|within\s+(?:1|one)\s+month|"
    r"this month|next 30 days?)\b",
    re.I,
)
_ONE_TO_THREE_MONTH_RE = re.compile(
    r"\b(?:within\s+(?:[123]|one|two|three)\s+months?|"
    r"(?:1|one)\s*(?:-|to)\s*(?:3|three)\s+months?|"
    r"next quarter|in a few months?)\b",
    re.I,
)
_LONG_TERM_RE = re.compile(
    r"\b(?:more than\s+3\s+months?|after\s+3\s+months?|"
    r"(?:4|5|6|7|8|9|10|11|12)\s+months?|just exploring|just browsing|browsing)\b",
    re.I,
)

_COMMITMENT_STRONG_RE = re.compile(
    r"\b(?:demo (?:is )?booked|booked (?:a )?demo|meeting (?:is )?booked|"
    r"scheduled (?:a )?(?:demo|meeting|call)|budget approved|approved budget|"
    r"decision[- ]?maker|i decide|final purchase decision|ready to (?:buy|proceed)|"
    r"want to proceed|let'?s proceed)\b",
    re.I,
)
_COMMITMENT_SOFT_RE = re.compile(
    r"\b(?:maybe later|follow up|follow-up|contact me|check back|let me think|"
    r"get back to me|reach out later)\b",
    re.I,
)

_CLARITY_STRONG_RE = re.compile(
    r"\b(?:workflow|process|conversion rate|roi|return on investment|metric|kpi|"
    r"response time|lead leakage|missed follow[- ]?ups?|sales process|"
    r"automate follow[- ]?ups?|qualify leads|centralized crm|manage leads)\b",
    re.I,
)
_CLARITY_BASIC_RE = re.compile(
    r"\b(?:slow repl(?:y|ies)|missed follow[- ]?ups?|leads? going cold|"
    r"tracking|crm|whatsapp|excel|sheets|lead management|real estate|education|"
    r"e-?commerce|retail|professional services)\b",
    re.I,
)
_DRIVES_CHAT_RE = re.compile(
    r"\b(?:demo|show me|explain|how does|how do|what is|pricing|price|cost|"
    r"can you|could you|want to|need to)\b",
    re.I,
)


def _norm(value) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _inbound_messages(lead) -> list[dict]:
    manager = getattr(lead, "whatsapp_messages", None)
    if manager is None:
        return []
    return list(
        manager.filter(direction="inbound")
        .order_by("created_at", "id")
        .values("id", "body")[:100]
    )


def _qualification_progress(qualification_state: dict, requirements: list[dict]) -> tuple[int, int, float]:
    required_ids = [
        str(item.get("id") or "")
        for item in requirements or []
        if item.get("required", True) and str(item.get("id") or "")
    ]
    states = qualification_state.get("requirement_states") or {}
    answered = sum(
        1
        for rid in required_ids
        if str((states.get(rid) or {}).get("status") or "").casefold() == "answered"
    )
    total = len(required_ids)
    ratio = (answered / total) if total else 0.0
    return answered, total, ratio


def _engagement_score(texts: list[str], answered_count: int) -> tuple[int, str]:
    meaningful = [text for text in texts if _norm(text) and _norm(text) not in _GENERIC_REPLIES]
    combined = " ".join(meaningful[-12:])
    words = re.findall(r"\b\w+\b", combined)
    question_count = sum(text.count("?") for text in meaningful[-12:])

    if (
        re.search(r"\b(?:book|schedule|request|want|need|show me)(?:\s+\w+){0,4}\s+(?:demo|call|meeting)\b", combined, re.I)
        or len(words) >= 45
        or (question_count >= 2 and len(words) >= 20)
    ):
        return 3, "Lead actively drove the conversation or supplied rich detail."
    if question_count >= 1 or len(words) >= 12 or _DRIVES_CHAT_RE.search(combined):
        return 2, "Lead asked questions or supplied useful detail."
    if answered_count > 0 or meaningful:
        return 1, "Lead answered questions but added limited extra context."
    return 0, "Replies were one-word or very generic."


def _urgency_score(combined: str) -> tuple[int, str]:
    if _ASAP_RE.search(combined):
        return 3, "Needs action this week or ASAP."
    if _WITHIN_MONTH_RE.search(combined):
        return 2, "Wants a solution within one month."
    if _ONE_TO_THREE_MONTH_RE.search(combined):
        return 1, "Timeline is one to three months or similarly vague."
    if _LONG_TERM_RE.search(combined):
        return 0, "Timeline is beyond three months or the lead is only browsing."
    return 0, "No clear urgency or timeline was provided."


def _clarity_score(combined: str) -> tuple[int, str]:
    strong_hits = len(set(match.group(0).casefold() for match in _CLARITY_STRONG_RE.finditer(combined)))
    if strong_hits >= 2 or re.search(r"\b(?:roi|metric|kpi|conversion rate)\b", combined, re.I):
        return 2, "Lead described a workflow, measurable outcome, or specific operational need."
    if _CLARITY_STRONG_RE.search(combined) or _CLARITY_BASIC_RE.search(combined):
        return 1, "Lead named a concrete use case or pain point."
    return 0, "Need remains vague with no specific pain point."


def _commitment_score(combined: str) -> tuple[int, str]:
    if _COMMITMENT_STRONG_RE.search(combined):
        return 2, "Lead supplied a firm buying or next-step signal."
    if _COMMITMENT_SOFT_RE.search(combined):
        return 1, "Lead supplied a soft follow-up or future-interest signal."
    return 0, "No clear next-step commitment was provided."


def calculate_lead_score(*, lead, qualification_state: dict, requirements: list[dict]) -> dict:
    messages = _inbound_messages(lead)
    texts = [str(item.get("body") or "").strip() for item in messages if str(item.get("body") or "").strip()]
    combined = " ".join(texts[-30:])

    answered, total_required, answer_ratio = _qualification_progress(
        qualification_state,
        requirements,
    )
    engagement, engagement_reason = _engagement_score(texts, answered)
    urgency, urgency_reason = _urgency_score(combined)
    clarity, clarity_reason = _clarity_score(combined)
    commitment, commitment_reason = _commitment_score(combined)

    raw_score = engagement + urgency + clarity + commitment
    eighty_percent_override = bool(total_required) and answer_ratio >= 0.8
    score = max(raw_score, QUALIFIED_SCORE) if eighty_percent_override else raw_score
    score = max(0, min(10, int(score)))

    return {
        "version": SCORE_VERSION,
        "score": score,
        "threshold": QUALIFIED_SCORE,
        "meets_qualification_threshold": score >= QUALIFIED_SCORE,
        "raw_score": raw_score,
        "eighty_percent_override": eighty_percent_override,
        "questions_answered": answered,
        "questions_required": total_required,
        "answered_ratio": round(answer_ratio, 4),
        "components": {
            "engagement": {"score": engagement, "max": 3, "reason": engagement_reason},
            "urgency_timeline": {"score": urgency, "max": 3, "reason": urgency_reason},
            "clarity_of_need": {"score": clarity, "max": 2, "reason": clarity_reason},
            "commitment_signal": {"score": commitment, "max": 2, "reason": commitment_reason},
        },
        "updated_at": timezone.now().isoformat(),
    }


def refresh_lead_score(*, lead, qualification_state: dict, requirements: list[dict], source_message_id=None) -> dict:
    score = calculate_lead_score(
        lead=lead,
        qualification_state=qualification_state,
        requirements=requirements,
    )
    score["source_message_id"] = str(source_message_id) if source_message_id is not None else None

    attributes = deepcopy(lead.attributes) if isinstance(getattr(lead, "attributes", None), dict) else {}
    attributes[LEAD_SCORE_KEY] = deepcopy(score)
    lead.__class__.objects.filter(pk=lead.pk).update(attributes=attributes)
    lead.attributes = attributes
    return score
