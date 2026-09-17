from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class IntentError(Exception):
    """Base error for safe intent-classification failures."""


class IntentScopeError(IntentError):
    """Caller supplied context that is not scoped to the supplied tenant."""


class Intent(str, Enum):
    GREETING = "GREETING"
    PRODUCT_OR_SERVICE_QUESTION = "PRODUCT_OR_SERVICE_QUESTION"
    PRICING_QUESTION = "PRICING_QUESTION"
    POLICY_QUESTION = "POLICY_QUESTION"
    LOCATION_QUESTION = "LOCATION_QUESTION"
    AVAILABILITY_QUESTION = "AVAILABILITY_QUESTION"
    QUALIFICATION_ANSWER = "QUALIFICATION_ANSWER"
    BOOKING_INTENT = "BOOKING_INTENT"
    CALL_REQUEST = "CALL_REQUEST"
    HUMAN_REQUEST = "HUMAN_REQUEST"
    OBJECTION = "OBJECTION"
    BUYING_INTENT = "BUYING_INTENT"
    FOLLOW_UP_RESPONSE = "FOLLOW_UP_RESPONSE"
    COMPLAINT = "COMPLAINT"
    OPT_OUT = "OPT_OUT"
    THANK_YOU = "THANK_YOU"
    AMBIGUOUS = "AMBIGUOUS"
    UNKNOWN = "UNKNOWN"


class ClassificationPath(str, Enum):
    DETERMINISTIC = "DETERMINISTIC"
    MODEL = "MODEL"
    FALLBACK = "FALLBACK"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class IntentDecision:
    primary_intent: Intent
    secondary_intents: tuple[Intent, ...] = ()
    confidence: float = 0.0
    entities: tuple[dict[str, Any], ...] = ()
    facts: tuple[dict[str, Any], ...] = ()
    direct_question: str | None = None
    qualification_candidate: dict[str, Any] | None = None
    requested_action: str | None = None
    classification_path: ClassificationPath = ClassificationPath.UNKNOWN
    language: str | None = None
    requires_knowledge: bool = False
    requires_human: bool = False
    classification_error: str | None = None
    model: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "primary_intent": self.primary_intent.value,
            "secondary_intents": [item.value for item in self.secondary_intents],
            "confidence": round(float(self.confidence), 4),
            "entities": [dict(item) for item in self.entities],
            "facts": [dict(item) for item in self.facts],
            "direct_question": self.direct_question,
            "qualification_candidate": (
                dict(self.qualification_candidate)
                if isinstance(self.qualification_candidate, dict)
                else None
            ),
            "requested_action": self.requested_action,
            "classification_path": self.classification_path.value,
            "language": self.language,
            "requires_knowledge": bool(self.requires_knowledge),
            "requires_human": bool(self.requires_human),
            "classification_error": self.classification_error,
            "model": self.model,
        }
