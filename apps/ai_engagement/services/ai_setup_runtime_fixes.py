from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


_INSTALLED = False

_OPTION_LINE_RE = re.compile(
    r"^\s*(?:[-*•]\s*)?(?P<key>[A-Za-z]|\d{1,2})\s*[\)\].:\-]\s+(?P<value>.+?)\s*$"
)
_INLINE_OPTION_RE = re.compile(
    r"(?<!\w)(?P<key>[A-Za-z]|\d{1,2})\s*[\)\].:\-]\s+"
)
_QUESTION_START_RE = re.compile(
    r"^(?:what|which|where|who|how|select|choose|share|tell)\b",
    flags=re.IGNORECASE,
)
_FREEFORM_TERMS = (
    "city",
    "location",
    "occupation",
    "profession",
    "course",
    "product",
    "service",
    "requirement",
    "name",
    "company",
    "destination",
    "country",
    "state",
    "industry",
    "role",
    "designation",
    "plan",
    "package",
    "model",
    "size",
    "type",
    "purpose",
)
_TYPED_TERMS = (
    "budget",
    "price range",
    "amount",
    "how much",
    "age",
    "quantity",
    "how many",
    "count",
    "timeline",
    "when",
    "date",
    "time frame",
    "timeframe",
    "how soon",
    "purchase by",
    "start by",
    "email",
    "e-mail",
    "phone",
    "mobile",
    "contact number",
    "whatsapp number",
)
_SHORT_NON_ANSWERS = {
    "hi",
    "hello",
    "hey",
    "hii",
    "good morning",
    "good afternoon",
    "good evening",
    "ok",
    "okay",
    "thanks",
    "thank you",
    "great",
    "fine",
    "sure",
    "done",
    "correct",
    "right",
}
_FILE_KNOWLEDGE_TERMS = {
    "brochure",
    "catalog",
    "catalogue",
    "pdf",
    "file",
    "document",
    "deck",
    "presentation",
    "menu",
    "prospectus",
    "portfolio",
    "flyer",
    "leaflet",
    "datasheet",
    "sheet",
    "price",
    "pricing",
    "pricelist",
    "rates",
}

_ADDITIONAL_ENGAGEMENT_INSTRUCTIONS = r"""
QUALIFICATION ANSWER AND ANTI-REPEAT RULES
- The application-selected NEXT_REQUIREMENT is the only new qualification
  requirement that may be asked on this turn.
- If the latest lead message answers the requirement that was just asked, treat
  that requirement as answered and continue to the next unresolved requirement.
  Never repeat the same answered requirement.
- A qualification requirement may contain explicit options. Accept the option
  letter, its lowercase form, the 1-based option number, or the written option
  text as the same answer. Example: A, a, 1, and the first option text all mean
  the first option.
- If the latest answer completes the final unresolved qualification requirement,
  do not ask another qualification question. Send a brief natural acknowledgment
  that the required details have been captured, while still answering any
  immediate supported customer question first.

PIPELINE STAGE TRANSITIONS
- pipeline.available_stages is the complete allow-list for stage movement.
- For non-Qualified destinations, use each destination stage description as the
  movement criterion. Propose at most one pipeline_transition only when current
  conversation evidence clearly satisfies that destination description.
- Never invent a stage id and never move a lead merely from vague positivity.
- The Qualified destination remains application-controlled and may be selected
  only when deterministic qualification evaluation permits it.
""".strip()


def _clean_spaces(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_option_key(value: str) -> str:
    text = str(value or "").strip()
    return text.upper() if text.isalpha() else text


def _looks_like_question(value: str) -> bool:
    text = _clean_spaces(value)
    if not text:
        return False
    return text.endswith("?") or bool(_QUESTION_START_RE.match(text))


def _extract_inline_options(value: str) -> tuple[str, list[dict[str, str]]] | None:
    text = str(value or "").strip()
    matches = list(_INLINE_OPTION_RE.finditer(text))
    if len(matches) < 2:
        return None

    prefix = text[: matches[0].start()].strip(" :-")
    options: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        option_value = text[start:end].strip(" ,;|/")
        if not option_value:
            continue
        options.append(
            {
                "key": _normalize_option_key(match.group("key")),
                "value": option_value,
            }
        )
    if len(options) < 2:
        return None
    return prefix, options


def _append_unique_option(block: dict[str, Any], key: str, value: str) -> None:
    normalized_key = _normalize_option_key(key)
    normalized_value = _clean_spaces(value)
    if not normalized_value:
        return
    existing = {
        (_normalize_option_key(item.get("key", "")), _clean_spaces(item.get("value", "")).casefold())
        for item in block["options"]
    }
    marker = (normalized_key, normalized_value.casefold())
    if marker not in existing:
        block["options"].append({"key": normalized_key, "value": normalized_value})


def _requirement_blocks(raw: str, organization_profile_module) -> list[dict[str, Any]]:
    text = str(raw or "").strip()
    if not text:
        return []

    compact = None
    if "\n" not in text and ";" not in text and "?" not in text:
        compact = organization_profile_module._split_compact_list(text)
    if compact:
        return [{"text": item, "options": []} for item in compact]

    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for raw_part in re.split(r"[\n;]+", text):
        original = str(raw_part or "").strip()
        if not original:
            continue

        inline = _extract_inline_options(original)
        if inline is not None:
            prefix, options = inline
            if prefix:
                cleaned_prefix = organization_profile_module._clean_requirement_line(prefix)
                current = {"text": cleaned_prefix, "options": []}
                for option in options:
                    _append_unique_option(current, option["key"], option["value"])
                blocks.append(current)
            elif current is not None:
                for option in options:
                    _append_unique_option(current, option["key"], option["value"])
            continue

        option_match = _OPTION_LINE_RE.match(original)
        if option_match is not None and current is not None:
            option_value = _clean_spaces(option_match.group("value"))
            if not option_value.endswith("?") and (
                current["options"] or _looks_like_question(current["text"])
            ):
                _append_unique_option(
                    current,
                    option_match.group("key"),
                    option_value,
                )
                continue

        cleaned = organization_profile_module._clean_requirement_line(original)
        if not cleaned:
            continue
        current = {"text": cleaned, "options": []}
        blocks.append(current)

    return blocks


def _enhanced_compile_qualification_requirements(raw: str) -> dict[str, Any]:
    from apps.ai_engagement.services import organization_profile as organization_profile_module

    mode = organization_profile_module._qualification_mode(raw)
    used: set[str] = set()
    requirements: list[dict[str, Any]] = []

    for block in _requirement_blocks(raw, organization_profile_module):
        text = _clean_spaces(block.get("text"))
        if not text or organization_profile_module._is_policy_line(text):
            continue

        options = [
            {
                "key": _normalize_option_key(item.get("key", "")),
                "value": _clean_spaces(item.get("value", "")),
            }
            for item in block.get("options") or []
            if _clean_spaces(item.get("value", ""))
        ]
        question_text = text
        if options:
            rendered_options = "\n".join(
                f"{item['key']}. {item['value']}" for item in options
            )
            question_text = f"{text}\n{rendered_options}"

        required = "optional" not in text.casefold()
        if mode == "all_required":
            required = True

        requirements.append(
            {
                "id": organization_profile_module._stable_requirement_id(text, used),
                "label": text.rstrip("?. "),
                "question": question_text,
                "required": required,
                "priority": len(requirements) + 1,
                "can_direct_ask": text.endswith("?") or bool(options),
                "options": options,
            }
        )

    return {
        "mode": mode,
        "requirements": requirements,
        "raw": str(raw or "").strip(),
    }


def _question_options(question: str) -> list[dict[str, str]]:
    text = str(question or "")
    options: list[dict[str, str]] = []
    for line in text.splitlines()[1:]:
        match = _OPTION_LINE_RE.match(line.strip())
        if match:
            options.append(
                {
                    "key": _normalize_option_key(match.group("key")),
                    "value": _clean_spaces(match.group("value")),
                }
            )
    if options:
        return options

    inline = _extract_inline_options(text)
    return inline[1] if inline is not None else []


def _match_option_answer(text: str, options: list[dict[str, str]]) -> str | None:
    raw = _clean_spaces(text)
    normalized = raw.casefold().strip(" .,:;-)('")
    normalized = re.sub(r"^option\s+", "", normalized)
    if not normalized:
        return None

    for index, option in enumerate(options, start=1):
        key = _normalize_option_key(option.get("key", ""))
        value = _clean_spaces(option.get("value", ""))
        if not value:
            continue
        aliases = {
            key.casefold(),
            str(index),
            chr(96 + index) if index <= 26 else "",
            value.casefold(),
            f"option {key}".casefold(),
            f"option {index}".casefold(),
        }
        aliases.discard("")
        if normalized in aliases:
            return value

        prefixed = re.match(r"^(?:option\s+)?([a-z]|\d{1,2})\b", normalized)
        if prefixed:
            supplied = prefixed.group(1).casefold()
            if supplied in {key.casefold(), str(index), chr(96 + index) if index <= 26 else ""}:
                return value
    return None


def _enhanced_direct_classifier(original_classifier, qualification_state_module):
    def classify(*, text: str, question: str):
        options = _question_options(question)
        if options:
            matched = _match_option_answer(text, options)
            if matched is not None:
                return (
                    qualification_state_module.REQUIREMENT_ANSWERED,
                    matched,
                    "high",
                )

        original = original_classifier(text=text, question=question)
        if original is not None:
            return original

        raw = str(text or "").strip()
        normalized = _clean_spaces(raw).casefold()
        if (
            not normalized
            or len(normalized) > 120
            or "?" in normalized
            or "\n" in raw
            or normalized in _SHORT_NON_ANSWERS
            or normalized in qualification_state_module._UNCLEAR_ONLY
        ):
            return None

        question_stem = _clean_spaces(str(question or "").splitlines()[0])
        lowered_question = question_stem.casefold()
        if any(term in lowered_question for term in _TYPED_TERMS):
            return None
        if qualification_state_module._is_boolean_question(question_stem):
            return None

        is_freeform_prompt = bool(_QUESTION_START_RE.match(question_stem)) or any(
            term in lowered_question for term in _FREEFORM_TERMS
        )
        if not is_freeform_prompt:
            return None

        return (
            qualification_state_module.REQUIREMENT_ANSWERED,
            raw,
            "high",
        )

    return classify


def _enhanced_evaluate_qualification(original_evaluator):
    def evaluate(*, runtime_policy: dict[str, Any], projected_state: dict[str, Any]):
        result = original_evaluator(
            runtime_policy=runtime_policy,
            projected_state=projected_state,
        )
        mode = str(
            ((runtime_policy.get("qualification") or {}).get("mode") or "configured")
        ).strip().casefold()
        if mode != "majority":
            return result

        required_results = [
            item for item in result.get("criteria", []) if item.get("required", True)
        ]
        if not required_results:
            return {**result, "outcome": "not_configured"}

        unresolved = [item for item in required_results if item.get("verdict") == "unknown"]
        if unresolved:
            return {**result, "outcome": "in_progress"}

        passes = sum(1 for item in required_results if item.get("verdict") == "pass")
        needed = (len(required_results) // 2) + 1
        return {
            **result,
            "outcome": "qualified" if passes >= needed else "not_qualified",
        }

    return evaluate


def _enhanced_controlled_actions(original_builder):
    def build(
        *,
        decision,
        context,
        runtime_policy: dict[str, Any],
        qualification_state: dict[str, Any],
        requirements: list[dict[str, Any]],
    ):
        controlled, result = original_builder(
            decision=decision,
            context=context,
            runtime_policy=runtime_policy,
            qualification_state=qualification_state,
            requirements=requirements,
        )

        # Deterministic Qualified movement always wins over a model proposal.
        if any(item.get("type") == "pipeline_transition" for item in controlled):
            return controlled, result

        pipeline = context.pipeline if isinstance(context.pipeline, dict) else {}
        available_stages = pipeline.get("available_stages") or []
        current_stage = context.stage if isinstance(context.stage, dict) else {}
        current_stage_id = str(current_stage.get("id") or "")
        by_id = {
            str(item.get("id")): item
            for item in available_stages
            if isinstance(item, dict) and item.get("id") is not None
        }

        for action in getattr(decision, "crm_actions", []) or []:
            if not isinstance(action, dict) or action.get("type") != "pipeline_transition":
                continue
            stage_shift = action.get("stage_shift")
            if not isinstance(stage_shift, dict):
                continue
            stage_id = str(stage_shift.get("stage_id") or "").strip()
            destination = by_id.get(stage_id)
            if not stage_id or destination is None or stage_id == current_stage_id:
                continue

            destination_name = _clean_spaces(destination.get("name")).casefold()
            if destination_name == "qualified":
                continue

            description = _clean_spaces(destination.get("description"))
            if not description:
                # Stage descriptions are the organization-authored transition
                # criteria. An undescribed stage is not safe for AI movement.
                continue

            controlled.append(
                {
                    "type": "pipeline_transition",
                    "stage_shift": {"stage_id": stage_id},
                }
            )
            result = {
                **result,
                "stage_transition": {
                    "stage_id": stage_id,
                    "source": "stage_description",
                },
            }
            break

        return controlled, result

    return build


def _enhanced_should_retrieve_knowledge(original_method):
    def should_retrieve(self, *, context):
        text = self._latest_inbound_text(context=context)
        normalized = _clean_spaces(text).casefold()
        words = set(re.findall(r"[a-z0-9]+", normalized))
        # Check knowledge/file intent before the legacy short-token fast path.
        # This fixes short messages such as "catalog", "brochure", "pdf" and
        # "price", which previously returned False before term matching ran.
        if words & (set(self._KNOWLEDGE_TERMS) | _FILE_KNOWLEDGE_TERMS):
            return True
        return original_method(self, context=context)

    return should_retrieve


def _normalize_question_for_repeat_check(value: str) -> str:
    first_line = str(value or "").splitlines()[0]
    return re.sub(r"[^a-z0-9]+", " ", first_line.casefold()).strip()


def _enhanced_qualification_validator(original_validator, engagement_module):
    def validate(
        self,
        *,
        decision,
        context,
        requirements,
        qualification_state,
    ):
        original_validator(
            self,
            decision=decision,
            context=context,
            requirements=requirements,
            qualification_state=qualification_state,
        )

        from apps.ai_engagement.services.qualification_state import (
            REQUIREMENT_ANSWERED,
            next_requirement,
            project_answer_updates,
        )

        projected = project_answer_updates(
            state=qualification_state,
            requirements=requirements,
            updates=getattr(decision, "qualification_updates", []) or [],
            messages=(context.conversation or {}).get("messages", []),
        )
        next_item = next_requirement(requirements, projected.get("requirement_states", {}))
        next_id = str(next_item.get("id") or "") if next_item else ""

        reason_code = str(getattr(decision, "reason_code", "") or "").strip().upper()
        if reason_code in {"QUALIFICATION_NEXT", "QUALIFICATION_CLARIFY"}:
            if not next_id:
                raise engagement_module.EngagementError(
                    "Qualification is complete. Acknowledge completion and do not ask another qualification question."
                )
            if str(getattr(decision, "next_requirement_id", "") or "") != next_id:
                raise engagement_module.EngagementError(
                    "Qualification response must ask only the application-selected NEXT_REQUIREMENT."
                )

        last_id = str(qualification_state.get("last_asked_requirement_id") or "").strip()
        latest_id = self._latest_inbound_message_id(context=context)
        last_state = (projected.get("requirement_states") or {}).get(last_id, {})
        answered_this_turn = bool(
            last_id
            and last_state.get("status") == REQUIREMENT_ANSWERED
            and str(last_state.get("source_message_id") or "") == str(latest_id or "")
        )
        if not answered_this_turn:
            return

        message = str(getattr(decision, "message", "") or "").strip()
        last_requirement = next(
            (
                item
                for item in requirements or []
                if str(item.get("id") or "") == last_id
            ),
            None,
        )
        if last_requirement:
            repeated = _normalize_question_for_repeat_check(
                str(last_requirement.get("question") or last_requirement.get("label") or "")
            )
            normalized_message = re.sub(
                r"[^a-z0-9]+", " ", message.casefold()
            ).strip()
            if repeated and repeated in normalized_message:
                raise engagement_module.EngagementError(
                    "Do not repeat a qualification question that the lead just answered."
                )

        if next_item is None:
            latest_text = self._latest_inbound_text(context=context)
            if "?" not in str(latest_text or "") and "?" in message:
                raise engagement_module.EngagementError(
                    "The final qualification answer is complete. Send an acknowledgment instead of another question."
                )

    return validate


def install_ai_setup_runtime_fixes() -> None:
    """Install deterministic AI Setup fixes without changing public contracts."""

    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import organization_profile as organization_profile_module
    organization_profile_module.compile_qualification_requirements = (
        _enhanced_compile_qualification_requirements
    )

    from apps.ai_engagement.services import qualification_state as qualification_state_module
    original_classifier = qualification_state_module._classify_direct_reply
    qualification_state_module._classify_direct_reply = _enhanced_direct_classifier(
        original_classifier,
        qualification_state_module,
    )

    from apps.ai_engagement.graph import policy_actions as policy_actions_module
    original_evaluator = policy_actions_module.evaluate_qualification
    policy_actions_module.evaluate_qualification = _enhanced_evaluate_qualification(
        original_evaluator
    )
    original_action_builder = policy_actions_module.build_controlled_actions
    policy_actions_module.build_controlled_actions = _enhanced_controlled_actions(
        original_action_builder
    )

    from apps.ai_engagement.services import engagement as engagement_module
    EngagementService = engagement_module.EngagementService
    EngagementService._KNOWLEDGE_TERMS = set(EngagementService._KNOWLEDGE_TERMS) | _FILE_KNOWLEDGE_TERMS
    original_should_retrieve = EngagementService._should_retrieve_knowledge
    EngagementService._should_retrieve_knowledge = _enhanced_should_retrieve_knowledge(
        original_should_retrieve
    )

    original_validator = EngagementService._validate_qualification_decision
    EngagementService._validate_qualification_decision = _enhanced_qualification_validator(
        original_validator,
        engagement_module,
    )

    if _ADDITIONAL_ENGAGEMENT_INSTRUCTIONS not in EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS:
        EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS = (
            f"{EngagementService.ENGAGEMENT_TASK_INSTRUCTIONS}\n\n"
            f"{_ADDITIONAL_ENGAGEMENT_INSTRUCTIONS}"
        )

    from apps.ai_engagement.prompts import engagement as engagement_prompt_module
    if _ADDITIONAL_ENGAGEMENT_INSTRUCTIONS not in engagement_prompt_module.CUSTOMER_ENGAGEMENT_INSTRUCTIONS:
        engagement_prompt_module.CUSTOMER_ENGAGEMENT_INSTRUCTIONS = (
            f"{engagement_prompt_module.CUSTOMER_ENGAGEMENT_INSTRUCTIONS}\n\n"
            f"{_ADDITIONAL_ENGAGEMENT_INSTRUCTIONS}"
        )

    _INSTALLED = True
