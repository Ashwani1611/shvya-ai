"""The single organization-authored operating specification for AI engagement.

Parsing is local and deterministic. Only the question section can create a
questionnaire; policy, acknowledgments and routing prose can never become a
customer-facing question accidentally.
"""
from __future__ import annotations

import re


SECTION_TITLES = {
    "rules": "Rules",
    "welcome_message": "Welcome Message",
    "qualification_questions": "Qualification Questions",
    "acknowledgment_message": "Acknowledgment Message",
    "qualification_criteria": "Qualification Criteria",
    "stage_shifting": "Stage shifting logic",
    "attribute_mapped": "Attribute mapping logic",
    "reminders": "Reminder creation logic",
}
SECTION_ALIASES = {
    "rules": {"rules", "rule", "engagement instructions"},
    "welcome_message": {"welcome message", "greeting", "greeting message"},
    "qualification_questions": {"qualification questions", "qualification requirements", "questions"},
    "acknowledgment_message": {"acknowledgment message", "acknowledgement message", "final acknowledgment message", "final acknowledgement message", "completion message"},
    "qualification_criteria": {"qualification criteria", "qualification criterion", "qualification rules", "qualification rule", "qualification"},
    "stage_shifting": {"stage shifting logic", "stage shifting", "stage shift", "stage movement", "stage routing", "pipeline shifting logic", "pipeline shifting", "pipeline shift", "pipeline routing"},
    "attribute_mapped": {"attribute mapping logic", "attribute mapped", "attributes mapped", "attribute mapping", "attribute mappings", "attribute map", "attribute filling", "attribute fill"},
    "reminders": {"reminder creation logic", "reminder creation", "reminder", "reminders", "reminder rules", "follow up reminder", "follow-up reminder"},
}
_HEADING = re.compile(r"^\s*#{1,6}\s*(.*?)\s*#*\s*$")


def parse_playbook(raw: str) -> dict[str, str]:
    buckets = {key: [] for key in SECTION_TITLES}
    current = "rules"
    section_depth = 0
    for line in str(raw or "").splitlines():
        line = re.sub(r"</?(?:welcome_message|question_content|acknowledg(?:e)?ment_message)>", "", line, flags=re.I)
        inline = re.match(r"^\s*(?:#{1,6}\s*)?(?:final\s+)?(welcome|acknowledg(?:e)?ment|completion)\s+message\s*:\s*(.+)$", line, re.I)
        if inline:
            current = "welcome_message" if inline.group(1).casefold() == "welcome" else "acknowledgment_message"
            buckets[current].append(inline.group(2).strip())
            continue
        heading = _HEADING.match(line)
        title = (heading.group(1) if heading else line).strip(" #:*._-").casefold()
        canonical = next((key for key, aliases in SECTION_ALIASES.items() if title in aliases), None)
        if canonical:
            current = canonical
            section_depth = len(line.lstrip()) - len(line.lstrip().lstrip("#")) if heading else 2
        elif heading:
            depth = len(line.lstrip()) - len(line.lstrip().lstrip("#"))
            if section_depth and depth > section_depth:
                # Only bare question-number labels are structure. Substantive
                # child headings remain authored content in their own section.
                content = heading.group(1)
                if current == "qualification_questions":
                    content = re.sub(r"^(?:question|q)\s*\d+\s*(?:[:.)-]\s*)?", "", content, flags=re.I).strip()
                if content:
                    buckets[current].append(content)
                continue
            # Unknown headings belong to general rules, never to the previous
            # question or CRM authority section.
            current = "rules"
            section_depth = depth
            buckets[current].append(line)
        else:
            buckets[current].append(line)
    return {key: "\n".join(lines).strip() for key, lines in buckets.items()}


def playbook_for_engagement(raw: str) -> str:
    """Keep policy available while question ordering stays in backend state."""
    sections = parse_playbook(raw)
    return "\n\n".join(
        f"## {title}\n{sections[key]}"
        for key, title in SECTION_TITLES.items()
        if key != "qualification_questions" and sections[key]
    )


def qualification_questions(raw: str) -> str:
    return parse_playbook(raw)["qualification_questions"]


def validate_playbook(raw: str) -> str:
    if not isinstance(raw, str):
        raise ValueError("AI Playbook must be text.")
    if len(raw) > 100000:
        raise ValueError("AI Playbook must be 100,000 characters or fewer.")
    # Compile at save time to surface malformed branching rather than making a
    # customer encounter a broken question later.
    from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
    compile_qualification_requirements(qualification_questions(raw))
    return raw.strip()


def evaluate_playbook_criteria(raw: str, *, requirements: list[dict], state: dict, values: dict | None = None) -> dict:
    """Prove authored criteria from persisted answers; unknown rules fail closed.

    Supports completion, numeric thresholds and exact values against question
    IDs/names. Unsupported prose is reported for configuration review, never
    silently treated as a passed requirement.
    """
    from apps.ai_engagement.graph.policy_actions import evaluate_condition

    text = parse_playbook(raw)["qualification_criteria"].strip()
    if not text:
        return {"qualified": False, "reason": "qualification_criteria_missing", "rules": []}
    states = dict(state.get("requirement_states") or {})
    requirements = list(requirements)
    for key, value in (values or {}).items():
        if str(key).startswith("_") or value in (None, ""):
            continue
        identifier = f"crm:{key}"
        requirements.append({"id": identifier, "label": str(key).replace("_", " "), "required": False})
        states[identifier] = {"status": "answered", "value": value}
    results = []
    for raw_rule in re.split(r"[\n;]+|(?<=\.)\s+|\s+and\s+(?=(?:[^\"\']|[\"\'][^\"\']*[\"\'])*$)", text, flags=re.I):
        rule = re.sub(r"^\s*(?:[-*•]+|\d+[.)])\s*", "", raw_rule).strip()
        if not rule:
            continue
        lower = rule.casefold().rstrip(".")
        completion = re.fullmatch(
            r"(?:(?:qualify|qualified|mark (?:the )?lead (?:as )?qualified|move (?:the )?lead to qualified)\s+(?:only\s+)?(?:when|if|after|once)\s+)?"
            r"(?:all|every)\s+(?:required\s+)?(?:qualification\s+)?(?:questions?|requirements?|answers?)\s+(?:must be\s+|are\s+|have been\s+)?(?:answered|complete|completed|captured|provided)(?:\s+(?:before qualifying|to qualify))?",
            lower,
        )
        if completion:
            required = [item for item in requirements if item.get("required", True)]
            passed = bool(required) and all(
                states.get(str(item.get("id")), {}).get("status") == "answered"
                or (states.get(str(item.get("id")), {}).get("status") == "not_applicable" and item.get("eligible_when"))
                for item in required
            )
            results.append({"rule": rule, "verdict": "pass" if passed else "unknown"})
            continue
        if re.search(r"\b(?:and|or|unless|except|not|never|without|if|when|after|before|until)\b", lower):
            results.append({"rule": rule, "verdict": "unknown"})
            continue
        references = []
        for item in requirements:
            priority = item.get("priority")
            aliases = [str(item.get("id") or "").replace("_", " "), str(item.get("stable_id") or ""), str(item.get("label") or "")]
            explicit = bool(priority and re.search(rf"\bq(?:uestion)?\s*{priority}\b", lower))
            tokens = set(re.findall(r"[a-z]+", str(item.get("label") or "").casefold())) - {"what", "which", "is", "are", "your", "the", "do", "you", "have", "how", "much", "many", "please", "share", "a", "an"}
            overlap = tokens & set(re.findall(r"[a-z]+", lower))
            if explicit or any(alias and len(alias) > 3 and alias.casefold() in lower for alias in aliases) or overlap:
                references.append((100 if explicit else len(overlap) + (0 if str(item.get("id", "")).startswith("crm:") else 10), item))
        references.sort(key=lambda pair: pair[0], reverse=True)
        item = references[0][1] if references and (len(references) == 1 or references[0][0] > references[1][0]) else None
        if item is None:
            results.append({"rule": rule, "verdict": "unknown"})
            continue
        answer = states.get(str(item.get("id")), {})
        condition = None
        subject = ""
        numeric = re.fullmatch(
            r"(?P<subject>.+?)\s*(>=|<=|>|<|=|(?:must be\s+)?(?:at least|at most|more than|greater than|above|over|less than|below|under|minimum|maximum))\s*[$₹]?\s*([\d,.]+\s*(?:k|lakh|lac|l|thousand)?)\.?", lower,
        )
        if numeric:
            from apps.ai_engagement.graph.runtime_policy import _number
            subject = numeric.group("subject")
            operator = numeric.group(2).removeprefix("must be ")
            operators = {">=": "gte", "<=": "lte", ">": "gt", "<": "lt", "=": "eq", "at least": "gte", "minimum": "gte", "at most": "lte", "maximum": "lte", "more than": "gt", "greater than": "gt", "above": "gt", "over": "gt", "less than": "lt", "below": "lt", "under": "lt"}
            condition = {"operator": "numeric_eq" if operator == "=" else operators[operator], "value": _number(numeric.group(3).replace("thousand", "k"))}
        equals = re.fullmatch(r"(?P<subject>.+?)\s+(?:equals?|must be|is)\s+(.+?)\.?", rule, re.I)
        if equals and not numeric:
            subject = equals.group("subject")
            target = equals.group(2).strip().strip("\"'")
            if not re.search(r"\b(?:at|least|most|greater|less|than|above|below|over|under|and|or|unless|except)\b", target, re.I):
                condition = {"operator": "eq", "value": target}
        present = re.fullmatch(r"(?P<subject>.+?)\s+(?:(?:must be|is|are|has been|have been)\s+)?(?:captured|provided|answered|known|present)\.?", lower)
        if present:
            subject = present.group("subject")
            condition = None
        # A scalar comparison cannot silently discard another predicate hidden
        # in its subject ("budget with manager approval >= 50000"). Resolve only
        # field/question references here; richer prose needs an evidenced receipt.
        subject_tokens = set(re.findall(r"[a-z0-9]+", subject.casefold()))
        reference_tokens = set(re.findall(r"[a-z0-9]+", " ".join(str(item.get(key) or "") for key in ("id", "stable_id", "label")).casefold()))
        reference_tokens |= {"the", "a", "an", "lead", "leads", "customer", "customers", "client", "clients", "must", "be", "is"}
        if item.get("priority"):
            reference_tokens |= {"q", "question", str(item["priority"]), f"q{item['priority']}"}
        verdict = "unknown"
        if subject_tokens and subject_tokens <= reference_tokens and answer.get("status") == "answered" and answer.get("value") not in (None, ""):
            verdict = evaluate_condition(answer.get("value"), condition) if condition else ("pass" if present else "unknown")
        results.append({"rule": rule, "verdict": verdict})
    return {"qualified": bool(results) and all(item["verdict"] == "pass" for item in results), "reason": "criteria_evaluated", "rules": results}


def criteria_for_lead(*, lead, state: dict | None = None, requirements=None) -> dict:
    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.transactional_turn_runtime import _requirements_for_turn
    from apps.ai_engagement.services.qualification_state import state_for_lead
    info = OrgInfo.objects.filter(organization_id=lead.organization_id).only("ai_playbook").first()
    active = requirements if requirements is not None else _requirements_for_turn(organization=lead.organization, lead=lead)
    values = {**(lead.attributes if isinstance(lead.attributes, dict) else {}), "name": lead.name, "phone": lead.phone}
    current_state = state if state is not None else state_for_lead(lead, requirements=active)
    raw = info.ai_playbook if info else ""
    result = evaluate_playbook_criteria(raw, requirements=active, state=current_state, values=values)
    if result["qualified"] or not result["rules"] or any(rule["verdict"] == "fail" for rule in result["rules"]):
        return result
    from apps.ai_engagement.services.semantic_criteria import verified_semantic_criteria_for_lead
    verified = verified_semantic_criteria_for_lead(lead, raw, active, current_state)
    return verified if verified is not None else result
