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
    sections = {key: "\n".join(lines).strip() for key, lines in buckets.items()}
    for key, tag in (("welcome_message", "welcome_message"),
                     ("qualification_questions", "question_content"),
                     ("acknowledgment_message", "acknowledg(?:e)?ment_message")):
        content, instructions = _message_content(sections[key], tag)
        sections[key] = content
        if instructions:
            sections["rules"] = (sections["rules"] + "\n" + instructions).strip()
    return sections


def _message_content(text: str, tag: str) -> tuple[str, str]:
    """Separate customer copy from policy, accepting paired and repeated tags.

    The documented format historically used two opening tags as delimiters.
    Outside text is still policy, never a trusted outgoing message suffix.
    """
    marker = re.compile(rf"</?{tag}>", re.I)
    matches = list(marker.finditer(text))
    if matches:
        content, policy = [], []
        inside = False
        start = 0
        for match in matches:
            (content if inside else policy).append(text[start:match.start()])
            inside = False if match.group().startswith("</") else not inside
            start = match.end()
        (content if inside else policy).append(text[start:])
        visible = []
        for part in content:
            customer, notes = _message_content(part, tag)
            if customer:
                visible.append(customer.strip('“”'))
            if notes:
                policy.append(notes)
        return "\n\n".join(visible), "\n".join(part.strip() for part in policy if part.strip())
    # Untagged copy remains supported. An explicit notes/instruction boundary
    # ends customer copy, including all continuation lines of that policy.
    boundary = re.search(r"(?im)^\s*(?:[-*]\s*)?(?:notes?|internal notes?|instructions?|rules?|do not tell the lead|send the qualification completion acknowledgment only once)\s*[:.]", text)
    if boundary:
        return text[:boundary.start()].strip(), text[boundary.start():].strip()
    return text.strip(), ""


def policy_blocks(text: str) -> list[str]:
    """Keep a numbered Rule/Mapping/Reminder and its children together."""
    blocks, current = [], []
    for line in text.splitlines():
        if re.match(r"^\s*(?:#{1,6}\s*)?(?:Rule|Mapping|Reminder)\s+\d+\b", line, re.I):
            if current:
                blocks.append("\n".join(current).strip())
            current = [line]
        elif current:
            current.append(line)
        elif line.strip():
            blocks.append(re.sub(r"^\s*[-*•]\s*", "", line).strip())
    if current:
        blocks.append("\n".join(current).strip())
    return blocks


def criteria_rules(text: str) -> list[str]:
    """Extract predicates without treating optional fields as requirements.

    Only recognized framing/advice is skipped; unrecognized conditions remain
    predicates and fail closed in the evaluator.
    """
    result = []
    optional = False
    clarification = False
    for line in text.splitlines():
        numbered = bool(re.match(r"^\s*\d+[.)]", line))
        rule = re.sub(r"^\s*(?:[-*•]+|\d+[.)])\s*", "", line).strip()
        if not rule:
            continue
        if numbered:
            optional = clarification = False
        if re.fullmatch(r"(?:a |the )?lead qualifies only when all (?:of )?these conditions are satisfied:", rule, re.I):
            continue
        if re.fullmatch(r"(?:the )?following attributes are optional and must not block qualification:", rule, re.I):
            optional = True
            continue
        if optional:
            continue
        if re.fullmatch(r"an answered question does not automatically mean its mapped value is valid\.?", rule, re.I):
            continue
        if re.fullmatch(r"if any required qualification value is unknown or unclear:", rule, re.I):
            clarification = True
            continue
        if clarification and re.fullmatch(r"(?:do not qualify the lead|ask for clarification when appropriate)\.?", rule, re.I):
            continue
        clarification = False
        result.append(rule)
    return result


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
    for raw_rule in re.split(r"[\n;]+|(?<=\.)\s+", "\n".join(criteria_rules(text)), flags=re.I):
        rule = re.sub(r"^\s*(?:[-*•]+|\d+[.)])\s*", "", raw_rule).strip()
        if not rule:
            continue
        lower = rule.casefold().rstrip(".")
        conditional = re.fullmatch(r"if\s+(.+?)(?:,?\s+then\s+|,\s*)(?:qualify(?: the lead)?|move (?:the )?lead to qualified)(?:\s*[,;]?\s*else\s+do not qualify(?: the lead)?)?\.?", rule, re.I)
        if conditional:
            rule = conditional.group(1)
            lower = rule.casefold().rstrip(".")
        # Boolean composition uses OR alternatives and AND requirements. Quoted
        # values stay intact; unsupported parentheses/exceptions fail closed.
        combined = False
        if not re.search(r"[()]|\b(?:unless|except)\b", rule, re.I):
            for operator in ("or", "and"):
                parts = re.split(rf"\s+{operator}\s+(?=(?:[^\"']|[\"'][^\"']*[\"'])*$)", rule, flags=re.I)
                if len(parts) < 2:
                    continue
                children = [evaluate_playbook_criteria('## Qualification Criteria\n' + part, requirements=requirements, state={"requirement_states": states}) for part in parts]
                verdicts = ["pass" if child["qualified"] else "fail" if any(item["verdict"] == "fail" for item in child["rules"]) else "unknown" for child in children]
                if operator == "or":
                    verdict = "pass" if "pass" in verdicts else "fail" if all(item == "fail" for item in verdicts) else "unknown"
                else:
                    verdict = "fail" if "fail" in verdicts else "pass" if all(item == "pass" for item in verdicts) else "unknown"
                results.append({"rule": raw_rule, "verdict": verdict})
                combined = True
                break
        if combined:
            continue
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
                exact_field = str(item.get("id", "")).startswith("crm:") and bool(re.match(rf"^{re.escape(str(item.get('label', '')).casefold())}\s", lower))
                references.append((200 if exact_field else 100 if explicit else len(overlap) + (0 if str(item.get("id", "")).startswith("crm:") else 10), item))
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
        present = re.fullmatch(r"(?P<subject>.+?)\s+(?:(?:(?:must be|is|are|has been|have been)\s+)?(?:captured|provided|answered|known|present)|has a clear value)\.?", lower)
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
        clear = str(answer.get("value", "")).strip().casefold() not in {"", "unknown", "unclear", "not sure", "none", "n/a"}
        if subject_tokens and subject_tokens <= reference_tokens and answer.get("status") == "answered" and clear:
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
    from apps.crm.models import AttributeDefinition
    for definition in AttributeDefinition.objects.filter(organization_id=lead.organization_id).values("name", "key"):
        if definition["key"] in values:
            values[definition["name"]] = values.pop(definition["key"])
    current_state = state if state is not None else state_for_lead(lead, requirements=active)
    raw = info.ai_playbook if info else ""
    result = evaluate_playbook_criteria(raw, requirements=active, state=current_state, values=values)
    if result["qualified"] or not result["rules"] or any(rule["verdict"] == "fail" for rule in result["rules"]):
        return result
    from apps.ai_engagement.services.semantic_criteria import verified_semantic_criteria_for_lead
    verified = verified_semantic_criteria_for_lead(lead, raw, active, current_state)
    return verified if verified is not None else result
