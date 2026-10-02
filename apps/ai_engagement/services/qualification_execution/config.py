"""Qualification configuration compilation and mapping rules."""

from __future__ import annotations

import re
from typing import Any

from .common import (
    _COMPLETION_RULE,
    _attribute_refs,
    _norm,
    _requirement_ref,
    _split_mapping,
    _strip_quotes,
)


def _config(
    *,
    organization,
    requirements: list[dict[str, Any]],
    raw_override: str | None = None,
) -> dict[str, Any]:
    """Compile deterministic qualification execution configuration for one org.

    raw_override is used only by no-write validation/dry-run callers so a
    proposed Playbook can be compiled before it becomes persisted state.
    Production callers continue reading the canonical OrgInfo Playbook.
    """
    from apps.ai_engagement.models import OrgInfo
    from apps.ai_engagement.services.engagement_instruction_policy import section_lines
    from apps.crm.models import AttributeDefinition, Stage

    info = OrgInfo.objects.filter(organization=organization).first()
    from apps.ai_engagement.services.playbook import parse_playbook
    raw = (
        str(raw_override)
        if raw_override is not None
        else str(getattr(info, "ai_playbook", "") or "")
    )
    sections = parse_playbook(raw)
    engagement_raw = raw
    definitions = list(
        AttributeDefinition.objects.filter(is_active=True, organization=organization).values(
            "key",
            "name",
            "field_type",
            "description",
            "options",
        )
    )

    mappings: dict[str, str] = {}
    mapping_targets: dict[str, list[str]] = {}
    value_rules: dict[str, str] = {}
    errors: list[dict[str, str]] = []

    mapping_lines = list(dict.fromkeys(section_lines(raw, "attribute_mapped")))

    for line in mapping_lines:
        pair = _split_mapping(line)
        if not pair:
            # Optional mappings from volunteered conversation facts are supplied
            # to the ordinary evidence-checked CRM path, not a question ID.
            if re.search(r"(?im)^\s*[-*]?\s*Attribute name:", line):
                continue
            errors.append(
                {
                    "type": "configuration_error",
                    "status": "failed",
                    "code": "invalid_attribute_mapping_rule",
                    "detail": line,
                }
            )
            continue
        requirement = _requirement_ref(pair[0], requirements)
        attributes, unresolved_attributes = _attribute_refs(pair[1], definitions)
        if requirement is None:
            errors.append(
                {
                    "type": "configuration_error",
                    "status": "failed",
                    "code": "unknown_requirement_mapping_reference",
                    "detail": pair[0],
                }
            )
            continue
        if unresolved_attributes:
            for unresolved in unresolved_attributes:
                errors.append(
                    {
                        "type": "configuration_error",
                        "status": "failed",
                        "code": "unknown_attribute_mapping_reference",
                        "detail": unresolved,
                    }
                )
        if not attributes:
            continue
        from apps.ai_engagement.services.confidentiality import (
            is_sensitive_attribute_definition,
        )

        sensitive = [
            attribute
            for attribute in attributes
            if is_sensitive_attribute_definition(attribute)
        ]
        if sensitive:
            errors.append(
                {
                    "type": "configuration_error",
                    "status": "failed",
                    "code": "sensitive_attribute_mapping",
                    "detail": pair[1],
                }
            )
            continue
        requirement_id = str(requirement.get("id") or "")
        targets = mapping_targets.setdefault(requirement_id, [])
        for attribute in attributes:
            attribute_key = str(attribute.get("key") or "")
            if attribute_key and attribute_key not in targets:
                targets.append(attribute_key)
            # Keep the first mapping as the backwards-compatible primary mapping
            # for older reconciliation callers. Qualification execution itself
            # uses the full one-to-many mapping_targets list.
            if attribute_key:
                mappings.setdefault(requirement_id, attribute_key)
                value_rules[attribute_key] = line

    final_ack = _strip_quotes(sections["acknowledgment_message"]) or None

    stages = list(
        Stage.objects.filter(
            pipeline__organization=organization,
            pipeline__is_active=True,
            is_active=True,
        ).values("id", "name", "pipeline_id", "pipeline__name")
    )
    stage_targets = []
    protected_completion_stage_ids = set()
    for line in section_lines(raw, "stage_shifting"):
        if not _COMPLETION_RULE.search(line):
            continue
        normalized = _norm(line)
        # Regression coverage: same-pipeline "Qualification" and "Qualified" must resolve distinctly.\n        # Resolve stage names from the transition target clause rather than the
        # whole qualification rule. Otherwise ordinary wording such as
        # "qualification questions" can accidentally match a stage literally
        # named "Qualification" while the intended target is "Qualified".
        target_match = re.search(
            r"\\b(?:move|shift|transition)(?:\\s+(?:the\\s+)?lead|\\s+it)?"
            r"(?:\\s+stage)?\\s+(?:to|into)\\s+(?P<target>.+)$",
            normalized,
            re.I,
        )
        target_text = target_match.group("target").strip() if target_match else normalized
        matches = [
            stage
            for stage in stages
            if _norm(stage["name"])
            and re.search(
                rf"(?<![a-z0-9]){re.escape(_norm(stage['name']))}(?![a-z0-9])",
                target_text,
            )
        ]
        # Prefer the most specific overlapping stage name in the explicit
        # target clause (for example "Lead Won" over a stage named "Lead").
        if target_match and len(matches) > 1:
            specific_matches = []
            for stage in matches:
                stage_name = _norm(stage["name"])
                if any(
                    stage_name != _norm(other["name"])
                    and stage_name
                    and stage_name in _norm(other["name"])
                    for other in matches
                ):
                    continue
                specific_matches.append(stage)
            matches = specific_matches or matches

        # Even an ambiguous completion rule must not let a model route to one
        # of its possible targets through the generic CRM action path.
        possible_targets = matches
        if len(matches) > 1:
            matches = [
                stage
                for stage in matches
                if _norm(stage["pipeline__name"])
                and _norm(stage["pipeline__name"]) in target_text
            ]
        protected_completion_stage_ids.update(str(stage["id"]) for stage in (matches or possible_targets))
        if len(matches) == 1:
            stage_targets.append(matches[0])
        else:
            errors.append(
                {
                    "type": "configuration_error",
                    "status": "failed",
                    "code": "unresolved_completion_stage_rule",
                    "detail": line,
                }
            )
    unique_targets = {str(stage["id"]): stage for stage in stage_targets}
    completion_stage = (
        next(iter(unique_targets.values())) if len(unique_targets) == 1 else None
    )
    if len(unique_targets) > 1:
        errors.append(
            {
                "type": "configuration_error",
                "status": "failed",
                "code": "ambiguous_completion_stage_rule",
                "detail": "Multiple completion targets configured.",
            }
        )

    return {
        "mappings": mappings,
        "mapping_targets": mapping_targets,
        "mapping_value_rules": value_rules,
        "final_ack": final_ack,
        "completion_stage": completion_stage,
        "protected_completion_stage_ids": sorted(protected_completion_stage_ids),
        "reminder_rules": section_lines(engagement_raw, "reminders"),
        "errors": errors,
    }


def _mapping_keys(config: dict[str, Any], requirement_id: str) -> list[str]:
    targets = (config.get("mapping_targets") or {}).get(str(requirement_id))
    if isinstance(targets, list):
        return [str(item).strip() for item in targets if str(item or "").strip()]
    primary = str((config.get("mappings") or {}).get(str(requirement_id)) or "").strip()
    return [primary] if primary else []


def _mapped_value(config, key, value):
    """Translate a proven answer only through explicit authored value arrows."""
    text = (config.get("mapping_value_rules") or {}).get(key, "")
    if not re.search(r"(?im)^\s*[-*]?\s*Attribute name:", text):
        return value
    targets = set()
    for line in text.splitlines():
        pair = _split_mapping(line.strip().lstrip("-* "))
        if not pair:
            continue
        source, target = pair
        source = source.strip('"“”')
        target = target.strip().rstrip(".").strip('"“”')
        aliases = [source, *re.split(r"\s*/\s*", source)]
        if _norm(value) == _norm(target) or any(_norm(value) == _norm(alias) for alias in aliases):
            targets.add(target)
    return next(iter(targets)) if len(targets) == 1 else value
