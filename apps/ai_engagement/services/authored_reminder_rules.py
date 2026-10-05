"""Conservative execution of structured, authored dated reminder rules."""
import re
from datetime import datetime

from django.utils import timezone

from .reminder_locales import (
    localized_request_blocked,
    localized_request_conditional,
    localized_request_kind,
    localized_time_ambiguous,
)


_REQUEST_NOUNS = r"(?:call|callback|phone|remind|reminder|follow[ -]?up|contact|connect)"
_NEGATED_REQUEST_RE = re.compile(
    rf"\b(?:do\s+not|don't|never|not|mat|nahi|nahin)\s+(?:please\s+|want\s+(?:a\s+)?|"
    rf"(?:set|create)\s+(?:a\s+)?)?{_REQUEST_NOUNS}\b|"
    rf"\b{_REQUEST_NOUNS}\s+(?:(?:me|mujhe|karna|karo|chahiye)\s+)?(?:mat|nahi|nahin|not)\b|"
    r"(?:मत|नहीं)\s*(?:कॉल|फोन)|(?:कॉल|फोन)\s*(?:मत|नहीं)", re.I,
)
_OPT_OUT_RE = re.compile(r"\b(?:unsubscribe|stop\s+(?:messaging|calling|contacting)|do\s+not\s+contact)\b", re.I)
_CALLBACK_REQUEST_RE = re.compile(
    r"\b(?:call\s+me|call\s+back|please\s+call|give\s+me\s+a\s+call|"
    r"connect\s+with\s+me|speak\s+with\s+me|"
    r"callback\s+(?:chahiye|karo|karna|please)|"
    r"(?:please|request|need|want|schedule|book|like)\s+(?:a\s+)?callback|"
    r"(?:call|phone)\s+(?:kar(?:o|na)?|kijiye|kar\s+(?:dena|lena)))\b|"
    r"^\s*callback\b|(?:मुझे.*(?:कॉल|फोन)|(?:कॉल|फोन)\s*(?:करो|करना|कीजिए))", re.I,
)
_FOLLOWUP_REQUEST_RE = re.compile(
    r"\b(?:follow[ -]?up\s+(?:with\s+me|kar(?:o|na)?|kijiye)|please\s+follow[ -]?up|"
    r"remind\s+me|(?:set|create)\s+(?:a\s+)?reminder|"
    r"reminder\s+(?:set|laga(?:o|na)?|kar(?:o|na)?)|yaad\s+dila(?:o|na|\s+dena)?)\b|"
    r"याद\s*दिला", re.I,
)


def _clause_request_kind(clause):
    if _NEGATED_REQUEST_RE.search(clause) or localized_request_blocked(clause):
        return None
    if _CALLBACK_REQUEST_RE.search(clause):
        return "callback"
    if _FOLLOWUP_REQUEST_RE.search(clause):
        return "later follow-up"
    return localized_request_kind(clause)


def reminder_request_evidence(text):
    """Keep the one positive request clause, excluding unrelated dated facts."""
    text = str(text or "")
    if _OPT_OUT_RE.search(text) or localized_request_conditional(text):
        return ""
    # Only explicit sentence/clause boundaries are used. Preserve dotted AM/PM
    # notation and append fragments containing only a supplied date/time.
    clauses = re.split(
        r"(?<=[.!?।])\s+|[;,]+|\b(?:and|aur|but|lekin|und|aber)\b|"
        r"(?<!\S)(?:आणि|पण|ਅਤੇ|ਪਰ|ಮತ್ತು|ಆದರೆ)(?!\S)", text, flags=re.I,
    )
    requests = []
    temporal_fragment = re.compile(
        r"^(?:(?:at|on|in|after|am|pm|a\.?m\.?|p\.?m\.?|utc|gmt|ist|india(?:n)?|time|"
        r"today|tomorrow|tonight|day|next|aaj|kal|parso[n]?|subah|shaam|sham|dopahar|raat|ko|baje|"
        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"one|two|three|four|five|six|seven|eight|nine|ten|ek|do|teen|char|paanch|"
        r"ghant[ae]|din|haft[ae]|baad|"
        r"minutes?|mins?|hours?|hrs?|days?|weeks?|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|"
        r"may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b|"
        r"[\d\s:+/.-])+$", re.I,
    )
    for index, clause in enumerate(clauses):
        if not _clause_request_kind(clause):
            continue
        evidence = clause.strip()
        for fragment in clauses[index + 1:]:
            fragment = fragment.strip()
            if re.match(r"^or\b", fragment, re.I) or localized_time_ambiguous(fragment):
                return ""  # An offered alternative is not an agreed callback.
            if not fragment or not temporal_fragment.fullmatch(fragment):
                break
            evidence += " " + fragment
        requests.append(evidence)
    return requests[0] if len(requests) == 1 else ""


def reminder_request_kind(text):
    """Conservative request recognition shared by authored and generic routing."""
    evidence = reminder_request_evidence(text)
    return _clause_request_kind(evidence) if evidence else None


def requested_reminder(*, rules, text, timezone_name=None):
    from apps.ai_engagement.services.reminder_time_runtime import parse_grounded_due_at

    # A dated statement alone is not a request. Unsupported conditions remain
    # on the evidence-checked model path rather than being guessed here.
    evidence = reminder_request_evidence(text)
    request_kind = _clause_request_kind(evidence)
    if request_kind is None:
        return None
    due = parse_grounded_due_at(evidence, timezone_name=timezone_name)
    requested = []
    for rule in rules:
        condition = re.search(r"(?im)^\s*[-*•]?\s*Create when\s+(.+?)\.?\s*$", rule)
        title = re.search(r"(?im)^\s*[-*•]?\s*Title:\s*(.+?)\.?\s*$", rule)
        if not condition or not title:
            continue
        match = re.fullmatch(
            r"the customer explicitly requests (?:a )?(callback|later follow-up) "
            r"(and provides or confirms a future date and time|immediately)", condition.group(1), re.I,
        )
        if not match or match.group(1).casefold() != request_kind:
            continue
        if match.group(2).casefold() == "immediately":
            if not re.search(r"\b(?:now|immediately|right now|abhi|turant)\b|अभी|तुरंत", evidence, re.I):
                continue
            rule_due = timezone.now().isoformat()
        else:
            if not due or datetime.fromisoformat(due) <= timezone.now():
                continue
            rule_due = due
        requested.append({"type": "create_reminder", "title": title.group(1).strip(),
                          "description": text.strip(), "due_at": rule_due})
    return requested[0] if len(requested) == 1 else None
