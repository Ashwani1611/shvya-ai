"""Conservative execution of structured, authored dated reminder rules."""
import re
from datetime import datetime

from django.utils import timezone


def requested_reminder(*, rules, text):
    from apps.ai_engagement.services.reminder_time_runtime import parse_grounded_due_at

    # A dated statement alone is not a request. Unsupported conditions remain
    # on the evidence-checked model path rather than being guessed here.
    if re.search(r"\b(?:not|never|stop|don't|do not|unsubscribe)\b", text, re.I):
        return None
    due = parse_grounded_due_at(text)
    if not due or datetime.fromisoformat(due) <= timezone.now():
        return None
    requested = []
    for rule in rules:
        condition = re.search(r"(?im)^\s*[-*•]?\s*Create when\s+(.+?)\.?\s*$", rule)
        title = re.search(r"(?im)^\s*[-*•]?\s*Title:\s*(.+?)\.?\s*$", rule)
        if not condition or not title:
            continue
        match = re.fullmatch(
            r"the customer explicitly requests (?:a )?(callback|later follow-up) "
            r"and provides or confirms a future date and time", condition.group(1), re.I,
        )
        if not match:
            continue
        pattern = (r"\b(?:call\s+me|callback|call\s+back|please\s+call)\b"
                   if match.group(1).casefold() == "callback"
                   else r"\b(?:follow[ -]?up|remind\s+me)\b")
        if re.search(pattern, text, re.I):
            requested.append({"type": "create_reminder", "title": title.group(1).strip(),
                              "description": text.strip(), "due_at": due})
    return requested[0] if len(requested) == 1 else None
