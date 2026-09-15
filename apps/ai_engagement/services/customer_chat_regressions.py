from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import replace
from functools import wraps


_INSTALLED = False
_INTERNAL_QUESTION_LABEL_RE = re.compile(
    r"^\s*Q\s*\d{1,3}\s*[\)\].:\-]\s*",
    flags=re.IGNORECASE,
)
_INTERNAL_QUESTION_LABEL_IN_MESSAGE_RE = re.compile(
    r"(^|[.!?]\s+)Q\s*\d{1,3}\s*[\)\].:\-]\s*",
    flags=re.IGNORECASE,
)
_INFORMATION_NOUN_TERMS = (
    "functionality",
    "functionalities",
    "function",
    "functions",
    "feature",
    "features",
    "capability",
    "capabilities",
    "benefit",
    "benefits",
    "integration",
    "integrations",
    "pricing",
    "price",
    "cost",
    "plan",
    "plans",
    "service",
    "services",
    "product",
    "products",
    "use case",
    "use cases",
    "details",
    "information",
)
_YES_VARIANTS = {
    "yes",
    "y",
    "yeah",
    "yep",
    "ye",
    "yea",
    "ya",
    "yup",
    "yes please",
    "correct",
}
_NO_VARIANTS = {"no", "n", "nope", "nah", "not yet"}


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def strip_internal_question_label(value: str) -> str:
    """Remove authored internal labels such as Q1./Q2) from customer text."""
    return _INTERNAL_QUESTION_LABEL_RE.sub("", str(value or ""), count=1).strip()


def _strip_internal_labels_from_message(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return _INTERNAL_QUESTION_LABEL_IN_MESSAGE_RE.sub(
        lambda match: match.group(1),
        text,
    ).strip()


def _strip_outer_customer_quotes(value: str) -> str:
    text = str(value or "").strip()
    if len(text) < 2:
        return text
    pairs = {
        '"': '"',
        "'": "'",
        "“": "”",
        "‘": "’",
    }
    closing = pairs.get(text[0])
    if closing and text.endswith(closing):
        inner = text[1:-1].strip()
        if inner:
            return inner
    return text


def sanitize_customer_message(value: str, *, qualification_context: bool = False) -> str:
    """Normalize formatting leakage without changing authored answer content."""
    text = _strip_outer_customer_quotes(value)
    if qualification_context:
        text = _strip_internal_labels_from_message(text)
    return text.strip()


def _information_noun_phrase(value: str) -> bool:
    normalized = _clean(value).casefold().strip(" .!?;:\"'()[]{}")
    if not normalized or len(normalized) > 180:
        return False
    words = normalized.split()
    if len(words) > 16:
        return False
    return any(term in normalized for term in _INFORMATION_NOUN_TERMS)


def _sanitize_requirement(value):
    if not isinstance(value, dict):
        return value
    item = deepcopy(value)
    if item.get("question") is not None:
        item["question"] = strip_internal_question_label(item.get("question"))
    if item.get("label") is not None:
        item["label"] = strip_internal_question_label(item.get("label"))
    return item


def _patch_requirement_compiler() -> None:
    from apps.ai_engagement.services import organization_profile

    if getattr(organization_profile, "_shvya_customer_label_patch", False):
        return

    original_clean = organization_profile._clean_requirement_line

    def clean_requirement_line(value: str) -> str:
        return strip_internal_question_label(original_clean(value))

    organization_profile._clean_requirement_line = clean_requirement_line
    organization_profile._shvya_customer_label_patch = True


def _patch_active_answer_normalization() -> None:
    from apps.ai_engagement.services import qualification_state

    if getattr(qualification_state, "_shvya_short_boolean_patch", False):
        return

    original_classifier = qualification_state._classify_direct_reply

    def classify(*, text: str, question: str):
        result = original_classifier(text=text, question=question)
        if result is not None:
            return result

        options = qualification_state._question_options(question)
        if not options:
            return None
        by_value = {
            _clean(item.get("value")).casefold(): _clean(item.get("value"))
            for item in options
            if _clean(item.get("value"))
        }
        if set(by_value) != {"yes", "no"}:
            return None

        normalized = _clean(text).casefold().strip(" .,:;!?()[]{}\"'")
        if normalized in _YES_VARIANTS:
            return (
                qualification_state.REQUIREMENT_ANSWERED,
                by_value["yes"],
                "high",
            )
        if normalized in _NO_VARIANTS:
            return (
                qualification_state.REQUIREMENT_ANSWERED,
                by_value["no"],
                "high",
            )
        return None

    qualification_state._classify_direct_reply = classify
    qualification_state._shvya_short_boolean_patch = True


def _patch_intent_priority() -> None:
    from apps.ai_engagement.services import conversation_priority_runtime

    if getattr(conversation_priority_runtime, "_shvya_noun_intent_patch", False):
        return

    original_intent_kind = conversation_priority_runtime._intent_kind

    def intent_kind(text: str) -> str:
        kind = original_intent_kind(text)
        if kind != "none":
            return kind
        if _information_noun_phrase(text):
            return "question"
        return "none"

    conversation_priority_runtime._intent_kind = intent_kind
    conversation_priority_runtime._shvya_noun_intent_patch = True


def _patch_engagement_runtime() -> None:
    from apps.ai_engagement.services.engagement import EngagementService

    if getattr(EngagementService, "_shvya_customer_chat_regression_patch", False):
        return

    original_should_retrieve = EngagementService._should_retrieve_knowledge
    original_build_input = EngagementService._build_input
    original_engage = EngagementService.engage

    def should_retrieve_knowledge(self, *, context):
        latest = self._latest_inbound_text(context=context)
        if _information_noun_phrase(latest):
            return True
        return original_should_retrieve(self, context=context)

    def build_input(self, *, context, **kwargs):
        raw = original_build_input(self, context=context, **kwargs)
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return raw

        turn = payload.get("qualification_turn")
        if isinstance(turn, dict):
            for key in (
                "current_requirement",
                "next_requirement_if_current_answered",
            ):
                turn[key] = _sanitize_requirement(turn.get(key))
            payload["qualification_turn"] = turn

        if isinstance(payload.get("next_requirement"), dict):
            payload["next_requirement"] = _sanitize_requirement(
                payload["next_requirement"]
            )

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @wraps(original_engage)
    def engage(self, *, organization, lead, knowledge_query=None, context=None):
        decision = original_engage(
            self,
            organization=organization,
            lead=lead,
            knowledge_query=knowledge_query,
            context=context,
        )
        message = str(getattr(decision, "message", "") or "")
        qualification_context = bool(
            str(getattr(decision, "next_requirement_id", "") or "").strip()
            or str(getattr(decision, "reason_code", "") or "").strip().upper()
            in {"QUALIFICATION_NEXT", "QUALIFICATION_CLARIFY"}
        )
        sanitized = sanitize_customer_message(
            message,
            qualification_context=qualification_context,
        )
        if sanitized != message.strip():
            return replace(decision, message=sanitized)
        return decision

    EngagementService._should_retrieve_knowledge = should_retrieve_knowledge
    EngagementService._build_input = build_input
    EngagementService.engage = engage
    EngagementService._shvya_customer_chat_regression_patch = True


def _patch_failsoft_capability_intent() -> None:
    from apps.ai_engagement.services import engagement_failsoft

    capability_terms = tuple(engagement_failsoft._CAPABILITY_TERMS)
    for term in ("function", "functions", "functionality", "functionalities"):
        if term not in capability_terms:
            capability_terms += (term,)
    engagement_failsoft._CAPABILITY_TERMS = capability_terms

    if getattr(engagement_failsoft, "_shvya_customer_chat_regression_patch", False):
        return

    original_builder = engagement_failsoft.build_deterministic_fallback_decision

    @wraps(original_builder)
    def build_deterministic_fallback_decision(*args, **kwargs):
        decision = original_builder(*args, **kwargs)
        message = str(getattr(decision, "message", "") or "")
        qualification_context = bool(
            str(getattr(decision, "next_requirement_id", "") or "").strip()
            or str(getattr(decision, "reason_code", "") or "").strip().upper()
            in {"QUALIFICATION_NEXT", "QUALIFICATION_CLARIFY"}
        )
        sanitized = sanitize_customer_message(
            message,
            qualification_context=qualification_context,
        )
        if sanitized != message.strip():
            return replace(decision, message=sanitized)
        return decision

    engagement_failsoft.build_deterministic_fallback_decision = (
        build_deterministic_fallback_decision
    )
    engagement_failsoft._shvya_customer_chat_regression_patch = True


def install_customer_chat_regressions() -> None:
    """Install narrow fixes for observed natural qualification chat failures."""
    global _INSTALLED
    if _INSTALLED:
        return

    _patch_requirement_compiler()
    _patch_active_answer_normalization()
    _patch_intent_priority()
    _patch_engagement_runtime()
    _patch_failsoft_capability_intent()
    _INSTALLED = True
