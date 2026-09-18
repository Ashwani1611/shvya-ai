"""Explicit customer facts and reported events; no additional extraction model."""
from __future__ import annotations

import re
from typing import Mapping

from apps.ai_engagement.services.sales_intelligence import settings_section

# Capture only self-reported phrases. A quoted business price is not a budget,
# and a requested appointment is not an appointment confirmed by SHVYA.
_FACT_RULES = {
    "budget": r"(?:\b(?:my|our)\s+budget\s*(?:is|of|:)?\s*|mera budget\s*|हमारा बजट\s*|मेरा बजट\s*)([₹$€£]?\s*\d[\d,]*(?:\.\d+)?(?:\s*(?:k|lakh|lakhs|thousand|million))?(?:\s*(?:inr|usd|rupees))?)",
    "timeline": r"\b(?:we|i)\s+(?:want|need|plan)\s+to\s+(?:start|launch|buy|implement)\s+((?:within|in|by|next)\s+[^.!?;,]{2,60})",
    "current_tools": r"\b(?:we|i|hum)\s+(?:currently\s+|already\s+)?(?:use|using|manage(?:\s+them)?\s+(?:in|with))\s+([\w .+\-]{2,50}?)(?=\s*(?:,|\.|\band\b|\baur\b|$))",
    "lead_volume": r"\b(?:receive|get|have|handle)\s+(?:around\s+|about\s+|approximately\s+)?(\d+)\s+(?:new\s+)?leads?\s+(?:daily|per day|every day)\b",
    "location": r"\b(?:we are|i am|we're|i'm)\s+(?:based|located)\s+in\s+([^.!?;,]{2,70})",
    "product_interest": r"\b(?:i am|we are|i'm|we're)\s+interested in\s+([^.!?;,]{2,100})",
    "preferred_contact_time": r"\b(?:please\s+)?(?:call|contact)\s+me\s+((?:tomorrow|today|on|at|after|before)\b[^.!?;]{0,70})",
    "preferences": r"\b(?:i|we)\s+prefer\s+([^.!?;,]{2,100})",
}
_RULES = {key: re.compile(value, re.I) for key, value in _FACT_RULES.items()}
_EVENT_RULES = {
    "appointment_reported": re.compile(r"\b(?:i|we)(?:'ve| have)? (?:already )?booked\b[^.!?]{0,160}|मेरी बुकिंग", re.I),
    "customer_commitment": re.compile(r"\b(?:i|we) will (?:send|share|pay|confirm|decide|buy)\b[^.!?]{0,160}", re.I),
    "customer_decision": re.compile(r"\b(?:i|we)(?:'ve| have)? decided\b[^.!?]{0,160}", re.I),
    "handoff_requested": re.compile(r"\b(?:speak|talk) (?:to|with) (?:a |your )?(?:human|person|agent|team)|\bcall me\b", re.I),
}


def explicit_facts(*, text, source_message_id, settings=None, definitions=(), language=None):
    config = settings_section(settings, "ai_memory")
    if config.get("enabled", True) is not True:
        return []
    text = str(text or "")[:12000]
    mappings = config.get("field_mappings")
    mappings = mappings if isinstance(mappings, Mapping) else {}
    valid = {str(item.get("key")) for item in definitions if isinstance(item, Mapping)}
    facts = []
    for concept, rule in _RULES.items():
        match = rule.search(text)
        if not match:
            continue
        value = match.group(1).strip()
        if concept == "lead_volume":
            value = int(value)
        target = mappings.get(concept)
        # Exact organization-owned definition only. Never fuzzy-match CRM keys.
        key = str(target) if isinstance(target, str) and target in valid else f"customer.{concept}"
        facts.append({"key": key, "concept": concept, "value": value,
                      "confidence": 0.95, "source_type": "explicit_customer",
                      "source_message_id": str(source_message_id), "evidence": match.group(0)[:500]})
    decision = re.search(r"\b(?:i am|i'm)\s+(not\s+)?(?:the\s+)?(?:decision maker|decision-maker)\b", text, re.I)
    if decision:
        facts.append({"key": "customer.decision_maker", "concept": "decision_maker",
                      "value": not bool(decision.group(1)), "confidence": 0.99,
                      "source_type": "explicit_customer", "source_message_id": str(source_message_id),
                      "evidence": decision.group(0)})
    # Language is an observation, not an explicit customer preference.
    if isinstance(language, str) and language.strip():
        facts.append({"key": "conversation.language", "concept": "language",
                      "value": language[:50], "confidence": 0.8,
                      "source_type": "conversation_extraction", "source_message_id": str(source_message_id),
                      "evidence": ""})
    return facts


def reported_events(text):
    text = str(text or "")[:12000]
    return [{"kind": kind, "text": match.group(0)[:300], "authority": "customer_report"}
            for kind, rule in _EVENT_RULES.items() if (match := rule.search(text))]
