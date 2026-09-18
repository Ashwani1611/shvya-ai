"""Deterministic, organization-configurable sales observations, not qualification.

Only explicit customer language / the existing intent result is observed. This
module never calls a provider, mutates CRM, confirms a booking, or invents offers.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Mapping

from apps.ai_engagement.services.intent_types import Intent, IntentDecision

CATEGORIES = (
    "PRICE_TOO_HIGH", "NEED_TIME", "COMPARING_OPTIONS", "NEED_APPROVAL", "NO_BUDGET",
    "NOT_NOW", "TRUST_CONCERN", "FEATURE_MISSING", "ALREADY_USING_ALTERNATIVE",
    "NOT_INTERESTED", "OTHER",
)
_PATTERNS = {
    "PRICE_TOO_HIGH": r"(?<!not )\b(?:too expensive|too costly|price is too high|bahut mehenga|bahut mahanga)\b|बहुत मह[ंँ]गा",
    "NEED_TIME": r"\b(?:need (?:some )?time|let me think|sochna padega|soch kar)\b|सोचने.*समय",
    "COMPARING_OPTIONS": r"\b(?:comparing (?:other )?options|compare with|looking at alternatives|aur options)\b|दूसरे विकल्प",
    "NEED_APPROVAL": r"\b(?:need (?:my )?(?:boss|manager|partner|team)?\s*approval|check with my (?:boss|partner)|approval chahiye)\b|मंजूरी चाहिए",
    "NO_BUDGET": r"\b(?:no budget|don't have (?:the )?budget|do not have (?:the )?budget|budget nahi|budget nahin)\b|बजट नहीं",
    "NOT_NOW": r"\b(?:not (?:right )?now|maybe later|abhi nahi|abhi nahin)\b|अभी नहीं",
    "TRUST_CONCERN": r"\b(?:can i trust|not sure i trust|is this (?:a )?scam|bharosa nahi)\b|भरोसा नहीं",
    "FEATURE_MISSING": r"\b(?:missing (?:a |the )?feature|doesn't support|does not support|feature missing)\b|सुविधा नहीं",
    "ALREADY_USING_ALTERNATIVE": r"\b(?:already (?:use|using)|already have (?:a |another )?(?:tool|system|provider))\b|पहले से.*इस्तेमाल",
    "NOT_INTERESTED": r"\b(?:not interested|no interest|interested nahi|interest nahi)\b|दिलचस्पी नहीं|रुचि नहीं",
}
_RULES = {key: re.compile(value, re.I) for key, value in _PATTERNS.items()}

DEFAULT_WEIGHTS = {
    "asked_pricing": 15, "requested_demo": 25, "requested_call": 15,
    "asked_availability": 10, "supplied_budget": 10, "supplied_timeline": 10,
    "product_question": 5, "repeated_engagement": 12, "requested_human": 5,
    "positive_commitment": 20, "negative_objection": -5,
    "not_interested": -30, "opt_out": -100,
}


def settings_section(settings, name):
    value = settings.get(name) if isinstance(settings, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def _strings(value, *, limit=20):
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip()[:1500] for item in value[:limit]
            if isinstance(item, str) and item.strip()]


@dataclass(frozen=True)
class Objection:
    category: str
    evidence: str
    confidence: float
    strategy: str = ""
    approved_facts: tuple[str, ...] = ()
    forbidden_claims: tuple[str, ...] = ()
    escalate: bool = False

    def as_dict(self):
        return asdict(self)


class ObjectionEngine:
    def detect(self, *, text, settings=None, intent_decision=None) -> tuple[Objection, ...]:
        config = settings_section(settings, "ai_objections")
        if config.get("enabled", True) is not True:
            return ()
        text = str(text or "")[:12000]
        rules = config.get("categories")
        rules = rules if isinstance(rules, Mapping) else {}
        matches = {}
        for category, rule in _RULES.items():
            match = rule.search(text)
            if match:
                matches[category] = match.group(0)
        # Tenant-authored phrases are literal strings, not arbitrary executable
        # regular expressions. No organization terminology is compiled globally.
        for category in CATEGORIES:
            item = rules.get(category)
            if not isinstance(item, Mapping):
                continue
            for phrase in _strings(item.get("phrases")):
                if len(phrase) >= 3 and re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text, re.I):
                    matches[category] = phrase
                    break
        intents = intent_set(intent_decision)
        if not matches and Intent.OBJECTION in intents:
            matches["OTHER"] = text[:500]
        output = []
        for category, evidence in matches.items():
            rule = rules.get(category)
            rule = rule if isinstance(rule, Mapping) else {}
            if rule.get("enabled", True) is not True:
                continue
            # Offers and discounts are permitted ONLY as explicit approved facts
            # supplied by this organization, never inferred from a category.
            facts = _strings(rule.get("approved_facts"))
            for field in ("offer", "discount"):
                value = rule.get(field)
                if isinstance(value, str) and value.strip():
                    facts.append(value.strip()[:1500])
            output.append(Objection(
                category=category, evidence=evidence[:500], confidence=0.95 if category != "OTHER" else 0.6,
                strategy=str(rule.get("strategy") or config.get("strategy") or "")[:2000],
                approved_facts=tuple(facts[:20]),
                forbidden_claims=tuple(_strings(config.get("forbidden_claims")) + _strings(rule.get("forbidden_claims"))),
                escalate=rule.get("escalate", False) is True,
            ))
        return tuple(output)


def intent_set(decision):
    if not isinstance(decision, IntentDecision):
        return set()
    return {decision.primary_intent, *decision.secondary_intents}


def detect_signals(*, text, intent_decision=None, facts=(), objections=(), repeated=False):
    """Return unique explainable observations; no numeric model quality score."""
    from apps.ai_engagement.services.intent_rules import deterministic_intents

    intents = intent_set(intent_decision) | deterministic_intents(str(text or "")[:12000])
    mapping = {
        Intent.PRICING_QUESTION: "asked_pricing", Intent.CALL_REQUEST: "requested_call",
        Intent.AVAILABILITY_QUESTION: "asked_availability",
        Intent.PRODUCT_OR_SERVICE_QUESTION: "product_question",
        Intent.HUMAN_REQUEST: "requested_human", Intent.OPT_OUT: "opt_out",
    }
    signals = {(kind, "") for intent, kind in mapping.items() if intent in intents}
    # Require a request/commitment, not an incidental mention of demo/payment.
    value = str(text or "")[:12000].casefold()
    if re.search(r"\b(?:book|schedule|request|want|need|show me)(?:\s+\w+){0,3}\s+demo\b|demo chahiye|डेमो चाहिए", value):
        signals.add(("requested_demo", ""))
    if re.search(r"\b(?:i|we)(?:'m| are| am)? (?:ready to (?:buy|proceed)|will (?:buy|purchase)|want to proceed)\b|आगे बढ़ना चाहता", value):
        signals.add(("positive_commitment", ""))
    for fact in facts:
        concept = str(fact.get("concept") or fact.get("key") or "").rsplit(".", 1)[-1]
        if concept in {"budget", "timeline"}:
            signals.add((f"supplied_{concept}", ""))
    for objection in objections:
        signals.add(("negative_objection", objection.category))
        if objection.category == "NOT_INTERESTED":
            signals.add(("not_interested", ""))
    if repeated:
        signals.add(("repeated_engagement", ""))
    return sorted(signals)


def score_signals(*, counts, settings=None):
    """Recompute from stored observations and current organization weights.

    Repeated messages cannot inflate one signal indefinitely. Caps/weights are
    bounded configuration, and each score component is returned for explanation.
    This function never reads or updates qualification status or a CRM stage.
    """
    config = settings_section(settings, "ai_signals")
    weights = config.get("weights")
    weights = weights if isinstance(weights, Mapping) else {}
    caps = config.get("caps")
    caps = caps if isinstance(caps, Mapping) else {}
    components = []
    for kind in sorted(DEFAULT_WEIGHTS):
        count = max(0, int(counts.get(kind) or 0))
        if not count:
            continue
        try:
            weight = float(weights.get(kind, DEFAULT_WEIGHTS[kind]))
            if not math.isfinite(weight):
                raise ValueError("non-finite weight")
            weight = max(-100.0, min(100.0, weight))
        except (ValueError, TypeError):
            weight = float(DEFAULT_WEIGHTS[kind])
        try:
            cap = max(0, min(20, int(caps.get(kind, 1))))
        except (ValueError, TypeError):
            cap = 1
        credited = min(count, cap)
        components.append({"signal": kind, "observed_count": count,
                           "credited_count": credited, "weight": weight,
                           "contribution": round(credited * weight, 2)})
    raw = round(sum(item["contribution"] for item in components), 2)
    return {"score": max(0.0, min(100.0, raw)), "unclamped_score": raw,
            "components": components, "authority": "deterministic_configured_weights",
            "qualification_override": False}
