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


def _completion_destinations(line, stages):
    """Resolve destination mentions, excluding a rule's source-stage name."""
    destinations = re.findall(
        r"\b(?:move|shift|route|transition)\b[^\n;]*?\b(?:to|into)\s+([^\n;]+)",
        str(line or ""), re.I,
    )
    target_text = " ".join(
        re.split(r"\s+(?:when|once|after|if|provided that)\b", item, maxsplit=1, flags=re.I)[0]
        for item in destinations
    ) if destinations else str(line or "")
    normalized = _norm(target_text)
    matches = [
        stage for stage in stages
        if _norm(stage["name"]) and re.search(
            rf"(?<![a-z0-9]){re.escape(_norm(stage['name']))}(?![a-z0-9])", normalized,
        )
    ]
    if destinations:
        # A stage named "Lead" or "Qualification" must not shadow the
        # explicitly authored "Lead Won" or "Qualification Complete".
        matches = [stage for stage in matches if not any(
            _norm(stage["name"]) != _norm(other["name"])
            and re.search(
                rf"(?<![a-z0-9]){re.escape(_norm(stage['name']))}(?![a-z0-9])",
                _norm(other["name"]),
            )
            for other in matches
        )]
    if re.search(r"\bcurrent pipeline\b", normalized):
        return matches, True

    # An unqualified stage name always belongs to the lead's current pipeline.
    # Every pipeline has a default Qualified stage, so organization-wide name
    # ambiguity is normal and must be resolved only when a lead is available.
    pipeline_scopes = set()
    for stage in matches:
        mention = re.search(re.escape(_norm(stage["name"])), normalized)
        suffix = normalized[mention.end():] if mention else ""
        scope = re.match(r"[\"'`]?\s+(?:in|within|into)\s+(?:the\s+)?(.+)", suffix)
        if scope:
            name = re.split(r"[.;\n]|\s+(?:when|once|after|if)\b", scope.group(1), maxsplit=1)[0]
            name = re.sub(r"^pipeline\s+|\s+pipeline$", "", name.strip()).strip("\"'` ")
            pipeline_scopes.add(name)
    if pipeline_scopes:
        # Resolve the whole pipeline name: "Missing Sales" cannot match Sales.
        return [stage for stage in matches if _norm(stage["pipeline__name"]) in pipeline_scopes], False
    return matches, True


def _completion_source_scope(line, source_choices):
    """Bind explicit acquisition-source predicates to CRM's source enum.

    This deliberately does not interpret the current messaging channel as the
    acquisition source. Unsupported source expressions stay unresolved.
    """
    aliases = {
        _norm(alias): key for key, label in source_choices
        for alias in (key, key.replace("_", " "), label)
    }
    choices = "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))
    pattern = re.compile(
        rf"\b(?:(?:lead[_ ]source|source)\s*(?:is|equals?|=|:)\s*|"
        rf"leads?\s+(?:created\s+|originating\s+)?from\s+(?:source\s+)?)"
        rf"[\"'`]*(?P<source>{choices})[\"'`]*(?![a-z0-9_])|"
        rf"\bfor\s+[\"'`]*(?P<for_source>{choices})[\"'`]*\s+leads?\b",
        re.I,
    )
    matches = list(pattern.finditer(str(line)))
    if matches:
        # Do not silently reduce a multi-source/negated predicate to one source.
        supported = len(matches) == 1 and not re.search(
            r"\b(?:not|never|unless|except)\b", str(line), re.I,
        ) and not re.match(r"\s+or\b", str(line)[matches[0].end():], re.I)
        return [aliases[_norm(match.group("source") or match.group("for_source"))] for match in matches], supported
    has_source_predicate = bool(re.search(
        r"\b(?:lead[_ ]source|source)\s*(?:is|equals?|=|:)|"
        r"\bleads?\s+(?:created|originating)\s+from\b|\bfor\s+\w+\s+leads?\b",
        str(line), re.I,
    ))
    return None, not has_source_predicate


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
    from apps.crm.models import AttributeDefinition, Lead, Stage

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
    current_pipeline_targets = []
    completion_routes = []
    protected_completion_stage_ids = set()
    completion_rules = [
        line for line in section_lines(raw, "stage_shifting")
        if _COMPLETION_RULE.search(line)
    ]
    for line in completion_rules:
        matches, current_pipeline_only = _completion_destinations(line, stages)
        sources, source_supported = _completion_source_scope(line, Lead._meta.get_field("lead_source").flatchoices)
        completion_routes.append({
            "targets": matches, "current_pipeline_only": current_pipeline_only,
            "sources": sources, "source_supported": source_supported,
        })
        protected_completion_stage_ids.update(str(stage["id"]) for stage in matches)
        if current_pipeline_only:
            # Organization compilation has no lead; bind at execution.
            current_pipeline_targets.append(matches)
        elif len(matches) == 1 and sources is None:
            stage_targets.append(matches[0])
        elif len(matches) != 1:
            errors.append({
                "type": "configuration_error", "status": "failed",
                "code": "unresolved_completion_stage_rule", "detail": line,
            })
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
        "timezone": str(getattr(organization, "timezone", "") or ""),
        "mappings": mappings,
        "mapping_targets": mapping_targets,
        "mapping_value_rules": value_rules,
        "final_ack": final_ack,
        "completion_stage": completion_stage,
        "completion_rules": completion_rules,
        "completion_routes": completion_routes,
        "current_pipeline_completion_targets": current_pipeline_targets,
        "protected_completion_stage_ids": sorted(protected_completion_stage_ids),
        "stage_rules": section_lines(engagement_raw, "stage_shifting"),
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
        normalized_value = _norm(value).translate(str.maketrans({"–": "-", "—": "-", "−": "-"}))
        normalized_aliases = [_norm(alias).translate(str.maketrans({"–": "-", "—": "-", "−": "-"})) for alias in aliases]
        # Authored numeric bands may include units/context, e.g. an answer
        # "11–30" mapped by "11–30 leads per day → 10-30". Only the same
        # complete numeric band may drop its trailing description.
        band = bool(re.fullmatch(r"\d+\s*-\s*\d+|\d+\+", normalized_value))
        if normalized_value == _norm(target) or any(
            normalized_value == alias or (band and alias.startswith(normalized_value + " "))
            for alias in normalized_aliases
        ):
            targets.add(target)
    return next(iter(targets)) if len(targets) == 1 else value
