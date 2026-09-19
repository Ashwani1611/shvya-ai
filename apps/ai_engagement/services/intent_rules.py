from __future__ import annotations

import re
from typing import Any, Iterable

from apps.ai_engagement.services.intent_types import Intent


EXACT_GREETING = {"hi", "hii", "hello", "hey", "hey there", "hello there", "namaste", "नमस्ते"}
EXACT_THANKS = {"thanks", "thank you", "thankyou", "thx", "धन्यवाद", "shukriya", "thank u"}
CONVERSATION_ACKS = {
    "yes", "yes please", "yeah", "yep", "yup", "sure", "okay", "ok", "correct",
    "right", "no", "nope", "not yet",
}
EXACT_OPT_OUT = {
    "stop", "unsubscribe", "remove me", "opt out", "don't message me", "dont message me",
    "do not message me", "stop messaging me", "stop contacting me", "don't contact me",
    "dont contact me",
}
KNOWLEDGE_INTENTS = {
    Intent.PRODUCT_OR_SERVICE_QUESTION,
    Intent.PRICING_QUESTION,
    Intent.POLICY_QUESTION,
    Intent.LOCATION_QUESTION,
    Intent.AVAILABILITY_QUESTION,
}
PRIMARY_PRIORITY = {
    Intent.OPT_OUT: 0,
    Intent.HUMAN_REQUEST: 10,
    Intent.CALL_REQUEST: 11,
    Intent.BOOKING_INTENT: 12,
    Intent.COMPLAINT: 20,
    Intent.PRICING_QUESTION: 30,
    Intent.POLICY_QUESTION: 31,
    Intent.LOCATION_QUESTION: 32,
    Intent.AVAILABILITY_QUESTION: 33,
    Intent.PRODUCT_OR_SERVICE_QUESTION: 34,
    Intent.BUYING_INTENT: 40,
    Intent.OBJECTION: 41,
    Intent.FOLLOW_UP_RESPONSE: 50,
    Intent.QUALIFICATION_ANSWER: 60,
    Intent.THANK_YOU: 70,
    Intent.GREETING: 71,
    Intent.AMBIGUOUS: 90,
    Intent.UNKNOWN: 100,
}
QUESTION_START = re.compile(
    r"^(?:what|which|where|when|why|how|who|can|could|would|will|is|are|do|does|did|have|has)\b",
    re.IGNORECASE,
)
GREETING_QUESTION = re.compile(
    r"^(?:hi+|hello|hey|namaste)\b.{0,80}?\b"
    r"(?P<question>what|which|where|when|why|how|who|can|could|would|will|is|are|do|does|did|have|has)\b"
    r"(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)
OPTION_LINE = re.compile(
    r"^\s*(?:[-*•]\s*)?(?P<key>[A-Za-z]|\d{1,2})\s*[\)\].:\-]\s+(?P<value>.+?)\s*$"
)
NUMBER = re.compile(r"(?<!\w)(\d+(?:\.\d+)?)(?!\w)")


def normalize(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().replace("’", "'").split())


def contains_any(text: str, terms: Iterable[str]) -> bool:
    return any(term in text for term in terms)


def detect_language(text: str) -> str | None:
    if re.search(r"[\u0900-\u097F]", text):
        return "hi"
    value = text.casefold()
    if re.search(r"\b(?:kya|hai|mujhe|baat|haan|nahi|hum|karte|karna|karo|kitna|kitne|chala)\b", value):
        return "hinglish"
    return "en" if re.search(r"[A-Za-z]", text) else None


def direct_question(text: str) -> str | None:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text) if p.strip()]
    questions = [p for p in parts if "?" in p or QUESTION_START.match(p)]
    if questions:
        return questions[-1][:500]
    value = text.strip()
    if QUESTION_START.match(value):
        return value[:500]

    # Leads often prefix a real question with a greeting or the assistant/brand
    # name, e.g. "Hi shvya what is shvya". Treat the interrogative clause as
    # the customer's question instead of allowing active qualification to
    # consume the whole message as a free-form answer.
    greeting_question = GREETING_QUESTION.match(value)
    if greeting_question:
        question = (
            f"{greeting_question.group('question')}{greeting_question.group('rest')}"
        ).strip()
        return question[:500] or None
    return None


def deterministic_intents(text: str) -> set[Intent]:
    value = normalize(text)
    compact = value.strip(" .!?;,:\"'")
    intents: set[Intent] = set()
    if compact in EXACT_GREETING:
        intents.add(Intent.GREETING)
    if compact in EXACT_THANKS:
        intents.add(Intent.THANK_YOU)
    if compact in EXACT_OPT_OUT or contains_any(
        value,
        ("unsubscribe", "remove me", "stop messaging", "stop contacting", "do not message", "don't message", "dont message", "opt out"),
    ):
        intents.add(Intent.OPT_OUT)
    mappings = (
        (Intent.PRICING_QUESTION, ("price", "pricing", "cost", "charges", "charge", "fee", "fees", "how much", "kitna charge", "kitne charge", "price kya", "cost kya")),
        (Intent.POLICY_QUESTION, ("refund policy", "refund", "cancellation policy", "cancel policy", "terms and conditions", "policy kya", "your policy", "policies")),
        (Intent.LOCATION_QUESTION, ("where are you", "where is your", "your location", "location kya", "located", "address kya", "your address", "office address")),
        (Intent.AVAILABILITY_QUESTION, ("availability", "available today", "available tomorrow", "are you available", "slot available", "slots available", "any slot", "open today")),
        (Intent.PRODUCT_OR_SERVICE_QUESTION, ("your service", "your services", "your product", "your products", "what do you do", "what exactly do", "know more about", "tell me about", "what does your", "how does your", "your features", "what features", "featurs", "featres", "why should i buy", "why would i buy", "why i buy", "why buy", "why should i choose", "why choose", "benefit", "benefits", "advantages")),
        (Intent.HUMAN_REQUEST, ("speak with someone", "speak to someone", "talk with someone", "talk to someone", "speak with a person", "speak to a person", "human agent", "real person", "connect me to someone", "connect me with someone", "person se baat", "kisi person se baat", "human se baat", "agent se baat")),
        (Intent.CALL_REQUEST, ("call me", "please call", "give me a call", "call back", "callback", "phone me", "mujhe call", "call karna", "call karo", "call kijiye")),
        (Intent.BOOKING_INTENT, ("book a", "book an", "book me", "schedule a", "schedule an", "book demo", "schedule demo", "book appointment", "schedule appointment")),
        (Intent.OBJECTION, ("too expensive", "very expensive", "quite expensive", "costly", "not interested", "not worth", "can't afford", "cannot afford", "budget is too")),
        (Intent.BUYING_INTENT, ("how can i get started", "how do i get started", "i want to get started", "sign me up", "how can i sign up", "i want to buy", "ready to buy", "i want to purchase", "ready to start")),
        (Intent.COMPLAINT, ("i'm unhappy", "im unhappy", "not happy with", "unhappy with", "not satisfied", "bad service", "terrible service", "poor service", "complaint about")),
        (Intent.FOLLOW_UP_RESPONSE, ("following up", "follow up", "follow-up", "you contacted me", "you messaged me")),
    )
    for intent, terms in mappings:
        if contains_any(value, terms):
            intents.add(intent)

    # Natural product-information requests are not always phrased as questions.
    # Keep specific pricing/policy/location/availability intents authoritative,
    # then treat broad "know about / functionality / capabilities" language as
    # product/service information instead of allowing active qualification to
    # consume it as a free-form answer.
    if (
        (
            contains_any(
                value,
                (
                    "want to know about",
                    "would like to know about",
                    "know about",
                    "functionality",
                    "functionalities",
                    "capabilities",
                    "what can you do",
                    "what do you offer",
                ),
            )
        )
        and not intents
        & {
            Intent.PRICING_QUESTION,
            Intent.POLICY_QUESTION,
            Intent.LOCATION_QUESTION,
            Intent.AVAILABILITY_QUESTION,
        }
    ):
        intents.add(Intent.PRODUCT_OR_SERVICE_QUESTION)

    # A lead asking "What is <brand/product>?" is an informational question,
    # even when they omit the question mark. Keep more specific question
    # families (pricing/policy/location/availability) authoritative.
    detected_question = normalize(direct_question(text) or "")
    if (
        detected_question.startswith("what is ")
        and not intents
        & {
            Intent.PRICING_QUESTION,
            Intent.POLICY_QUESTION,
            Intent.LOCATION_QUESTION,
            Intent.AVAILABILITY_QUESTION,
        }
    ):
        intents.add(Intent.PRODUCT_OR_SERVICE_QUESTION)
    return intents


def question_options(question: str) -> list[dict[str, str]]:
    result = []
    for line in str(question or "").splitlines()[1:]:
        match = OPTION_LINE.match(line.strip())
        if match:
            key = match.group("key")
            result.append({
                "key": key.upper() if key.isalpha() else key,
                "value": re.sub(r"\s+", " ", match.group("value")).strip(),
            })
    return result


def match_option(text: str, options: list[dict[str, str]], *, allow_key: bool) -> str | None:
    value = normalize(text).strip(" .,:;-)('\"")
    stripped = re.sub(r"^option\s+", "", value)
    for index, option in enumerate(options, start=1):
        key = normalize(option.get("key"))
        authored = str(option.get("value") or "").strip()
        authored_norm = normalize(authored)
        if allow_key:
            aliases = {key, str(index), f"option {key}", f"option {index}"}
            if index <= 26:
                aliases.add(chr(96 + index))
            if value in aliases or stripped in aliases:
                return authored
        if value == authored_norm or authored_norm in value:
            return authored
        words = [w for w in re.findall(r"[a-z0-9]+", authored_norm) if len(w) >= 4]
        if len(words) >= 2 and sum(w in value for w in words) >= min(2, len(words)):
            return authored
    return None


def is_numeric_question(question: str) -> bool:
    return any(
        re.search(rf"\b{re.escape(term)}\b", question)
        for term in ("how many", "number of", "count", "volume", "daily", "per day", "budget", "amount", "age", "quantity")
    )


def numeric_candidate(text: str, question: str) -> int | float | None:
    value = normalize(text)
    matches = list(NUMBER.finditer(value))
    if not matches:
        return None
    if not (
        contains_any(value, ("lead", "daily", "per day", "every day", "around", "about", "approx", "approximately"))
        or (len(matches) == 1 and len(value.split()) <= 8)
        or contains_any(question, ("budget", "age", "amount"))
    ):
        return None
    number = float(matches[0].group(1))
    return int(number) if number.is_integer() else number


def is_boolean_question(question: str) -> bool:
    return question.strip().startswith((
        "is ", "are ", "do ", "does ", "did ", "have ", "has ", "can ",
        "could ", "would ", "will ", "was ", "were ",
    )) or " yes/no" in question or "yes or no" in question


def boolean_candidate(text: str, question: str) -> bool | None:
    value = normalize(text)
    yes = bool(re.search(r"\b(?:yes|yeah|yep|haan|ha|han|जी हाँ|हाँ)\b", value))
    no = bool(re.search(r"\b(?:no|nope|nahi|nahin|नहीं)\b", value))
    intermittent = bool(
        re.search(
            r"\b(?:sometimes?|some\s+times?|occasionally|at\s+times|"
            r"from\s+time\s+to\s+time|on\s+and\s+off|off\s+and\s+on|"
            r"once\s+in\s+a\s+while|rarely)\b",
            value,
        )
    )
    if intermittent and not re.search(r"\b(?:not|never|no\s+longer)\b", value):
        yes = True
    if yes == no:
        return None
    keywords = [
        token for token in re.findall(r"[a-z]{3,}", question)
        if token not in {"are", "you", "your", "the", "and", "with", "have", "has", "does", "do", "is"}
    ]
    if (
        not intermittent
        and len(value.split()) > 3
        and keywords
        and not any(token in value for token in keywords[:5])
    ):
        if not ("ad" in question and re.search(r"\bads?\b", value)):
            return None
    return yes


def generic_candidate(text: str, question: str, *, active: bool) -> tuple[Any, float, str] | None:
    if normalize(text).strip(" .!?;,:\"'") in CONVERSATION_ACKS:
        return None
    if contains_any(question, ("tool", "software", "crm", "system", "manage", "track", "platform")):
        match = re.search(
            r"\b(?:we|i|hum)\s+(?:currently\s+)?(?:use|using|manage(?:\s+them)?\s+(?:in|with))\s+([A-Za-z][A-Za-z0-9 ._+-]{1,40}?)(?=\s*(?:,|\.|\band\b|\baur\b|$))",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(1).strip(), 0.9, "natural_text"
    if (
        active
        and len(text) <= 180
        and not direct_question(text)
        and not deterministic_intents(text)
    ):
        if contains_any(question, ("what", "which", "city", "location", "service", "product", "goal", "challenge", "problem", "source", "industry", "role", "type")):
            return text.strip(), 0.75, "active_freeform"
    return None


def qualification_facts(
    *,
    text: str,
    requirements: list[dict[str, Any]],
    qualification_state: dict[str, Any],
    source_message_id: str | None,
) -> list[dict[str, Any]]:
    active_id = str(
        qualification_state.get("current_requirement_id")
        or qualification_state.get("last_asked_requirement_id")
        or qualification_state.get("next_requirement_id")
        or ""
    ).strip()
    normalized_reply = normalize(text).strip(" .!?;,:\"'")
    bare_boolean_reply = normalized_reply in {
        "yes", "yeah", "yep", "y", "haan", "ha", "han",
        "no", "nope", "n", "nahi", "nahin",
    }
    bare_numeric_reply = bool(re.fullmatch(r"\d+(?:\.\d+)?", normalized_reply))
    facts = []
    for requirement in requirements:
        rid = str(requirement.get("id") or "").strip()
        question = str(requirement.get("question") or requirement.get("label") or "").strip()
        if not rid or not question:
            continue
        value = None
        confidence = 0.0
        kind = ""
        options = question_options(question)
        if options:
            value = match_option(text, options, allow_key=(rid == active_id))
            if value is not None:
                confidence, kind = 0.99, "configured_option"
        first_line = normalize(question.splitlines()[0])
        if (
            value is None
            and is_numeric_question(first_line)
            and not (rid != active_id and bare_numeric_reply)
        ):
            value = numeric_candidate(text, first_line)
            if value is not None:
                confidence, kind = (0.97 if rid == active_id else 0.9), "numeric"
        if (
            value is None
            and is_boolean_question(first_line)
            and not (rid != active_id and bare_boolean_reply)
        ):
            value = boolean_candidate(text, first_line)
            if value is not None:
                confidence, kind = (0.96 if rid == active_id else 0.88), "boolean"
        if value is None:
            generic = generic_candidate(text, first_line, active=(rid == active_id))
            if generic is not None:
                value, confidence, kind = generic
        if value is None:
            continue
        facts.append({
            "key": rid,
            "requirement_id": rid,
            "label": str(requirement.get("label") or question.splitlines()[0]).strip(),
            "value": value,
            "evidence": text,
            "confidence": round(confidence, 4),
            "kind": kind,
            "source_message_id": str(source_message_id or "") or None,
        })
    return facts


def ordered_intents(intents: set[Intent]) -> tuple[Intent, tuple[Intent, ...]]:
    cleaned = {i for i in intents if i not in {Intent.UNKNOWN, Intent.AMBIGUOUS}}
    if not cleaned:
        cleaned = set(intents) or {Intent.UNKNOWN}
    ordered = sorted(cleaned, key=lambda i: (PRIMARY_PRIORITY[i], i.value))
    return ordered[0], tuple(ordered[1:])


def preferred_candidate(facts: list[dict[str, Any]], state: dict[str, Any]) -> dict[str, Any] | None:
    if not facts:
        return None
    active = str(
        state.get("current_requirement_id")
        or state.get("last_asked_requirement_id")
        or state.get("next_requirement_id")
        or ""
    ).strip()
    item = next((f for f in facts if str(f.get("requirement_id") or "") == active), facts[0])
    return {
        "requirement_id": item.get("requirement_id") or item.get("key"),
        "value": item.get("value"),
        "evidence": item.get("evidence"),
        "confidence": item.get("confidence"),
    }


def requested_action(intents: set[Intent]) -> str | None:
    for intent, action in (
        (Intent.OPT_OUT, "OPT_OUT"),
        (Intent.HUMAN_REQUEST, "HUMAN_HANDOFF"),
        (Intent.CALL_REQUEST, "CALL"),
        (Intent.BOOKING_INTENT, "BOOKING"),
    ):
        if intent in intents:
            return action
    return None
