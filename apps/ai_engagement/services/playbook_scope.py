"""Apply explicit source/channel predicates without inventing a second Playbook.

Only applicability is resolved here. Existing condition, evidence, qualification
and action validators still authorize every effect. Unresolved scopes fail closed.
"""
from __future__ import annotations

import re
from copy import deepcopy


_CHANNEL = re.compile(r"\b(?:chat\s+channel|messaging\s+channel|channel)\s*(?:is|equals?|=|:)\s*[\"'`]*(\w+)", re.I)
_ACQUISITION = re.compile(
    r"\blead[_ ]source\s*(?:is|equals?|=|:)|\bsource\s+(?:is|equals?)\b|"
    r"\bleads?\s+(?:created\s+|originating\s+)?from\b|\bfor\s+[\w ]+\s+leads?\b", re.I,
)


def rule_applies(rule, *, lead_source=None, channel=None):
    """Reuse the existing conservative source parser and the actual CRM enum."""
    if not isinstance(rule, str):
        return False
    if _ACQUISITION.search(rule):
        from apps.ai_engagement.services.qualification_execution.config import _completion_source_scope
        from apps.crm.models import Lead
        # Mapping metadata 'Source: Q1' is not an acquisition-source predicate.
        scoped = re.sub(r"(?im)^\s*[-*]?\s*source\s*:\s*(?:q\d+|(?:qualification\s+)?question\s+\d+|latest\s+(?:inbound\s+)?message)[^\n]*", "", rule)
        sources, supported = _completion_source_scope(scoped, Lead._meta.get_field("lead_source").flatchoices)
        if not supported or (sources is not None and str(lead_source or "") not in sources):
            return False
    channels = list(_CHANNEL.finditer(rule))
    if channels:
        if len(channels) != 1 or re.search(r"\b(?:not|never|unless|except)\b", rule, re.I):
            return False
        selected = channels[0].group(1).casefold()
        if selected not in {"instagram", "whatsapp", "sandbox"} or selected != str(channel or "").casefold():
            return False
        if re.match(r"\s+or\b", rule[channels[0].end():], re.I):
            return False
    elif re.search(r"\b(?:chat\s+channel|messaging\s+channel|channel)\s*(?:is|equals?|=|:)", rule, re.I):
        return False
    return True


def scoped_runtime_policy(policy, context):
    policy = deepcopy(policy)
    lead_source = (context.lead or {}).get("lead_source")
    channel = (context.conversation or {}).get("channel")
    crm = policy.get("crm") or {}
    omitted = {}
    for key in ("attribute_mapped", "stage_shifting", "reminders"):
        rules = crm.get(key)
        if not isinstance(rules, list):
            continue
        applicable = [rule for rule in rules if rule_applies(rule, lead_source=lead_source, channel=channel)]
        omitted[key] = len(rules) - len(applicable)
        crm[key] = applicable
    policy["crm"] = crm
    policy["applicability"] = {"lead_source": lead_source, "channel": channel,
                               "excluded_rule_counts": omitted, "authorizes_actions": False}
    return policy


def scoped_file_candidates(candidates, context):
    return [item for item in candidates or [] if isinstance(item, dict) and rule_applies(
        item.get("share_instruction", ""), lead_source=(context.lead or {}).get("lead_source"),
        channel=(context.conversation or {}).get("channel"),
    )]
