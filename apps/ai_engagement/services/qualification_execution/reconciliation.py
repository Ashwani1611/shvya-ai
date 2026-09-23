"""Build response plans from reconciled, persisted CRM state."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import replace
from typing import Any

from .common import (
    _ACK_LABEL,
    _GENERIC_ACKS,
    _LABEL_ONLY,
    _QUESTION_FRAGMENT_RE,
    _clean,
    _norm,
    _strip_quotes,
)
from .completion import _completion_target
from .config import _config, _mapped_value, _mapping_keys
from .evidence import _verify_attribute, _verify_stage
from .planning import _plan


def _plan_from_reconciled(*, lead, source_message_id, snapshot):
    """Build the final response plan from actual persisted state after model interpretation."""
    from apps.ai_engagement.services import qualification_state as qs
    from apps.ai_engagement.services.transactional_turn_runtime import _requirements_for_turn

    requirements = _requirements_for_turn(
        organization=lead.organization,
        lead=lead,
    )
    source = (
        lead.whatsapp_messages.filter(
            pk=source_message_id,
            direction="inbound",
        )
        .only("body")
        .first()
    )
    if source is None or not requirements:
        return None

    state = qs.state_for_lead(lead, requirements=requirements)
    answered = [
        (requirement_id, item)
        for requirement_id, item in (state.get("requirement_states") or {}).items()
        if isinstance(item, dict)
        and str(item.get("source_message_id") or "") == str(source_message_id)
        and _norm(item.get("status")) == "answered"
    ]
    if not answered:
        return None

    config = _config(
        organization=lead.organization,
        requirements=requirements,
    )
    results = [
        deepcopy(item)
        for item in snapshot.get("execution_results") or []
        if isinstance(item, dict)
    ]

    # Verify every explicitly mapped attribute target by DB readback for every
    # answer persisted by this inbound message, regardless of model proposals.
    for requirement_id, item in answered:
        for attribute_key in _mapping_keys(config, str(requirement_id)):
            verification = _verify_attribute(
                lead,
                attribute_key,
                _mapped_value(config, attribute_key, item.get("value")),
            )
            results = [
                result
                for result in results
                if not (
                    result.get("type") == "attribute_updates"
                    and any(
                        str(update.get("key") or "") == attribute_key
                        for update in result.get("updates") or []
                        if isinstance(update, dict)
                    )
                )
            ]
            results.append(verification)

    target = _completion_target(lead=lead, state=state, config=config)
    runtime_config = {**config, "completion_stage": target}
    if target and _norm(state.get("qualification_status")) == "completed":
        verification = _verify_stage(lead, target)
        results = [
            result
            for result in results
            if result.get("type") != "pipeline_transition"
        ]
        results.append(verification)

    return _plan(
        source=source,
        answer=answered[-1][1].get("value"),
        state=state,
        requirements=requirements,
        config=runtime_config,
        results=results,
    )


def _fallback_acknowledgement(plan: dict[str, Any]) -> str:
    """Build a short answer-aware acknowledgement from backend-owned facts."""
    context = plan.get("acknowledgement_context")
    context = context if isinstance(context, dict) else {}
    answer = context.get("normalized_answer")
    if isinstance(answer, bool):
        answer_text = "Yes" if answer else "No"
    else:
        answer_text = _clean(answer or context.get("raw_answer"))
    answer_text = answer_text.strip(" .!?")
    question = _norm(context.get("question"))

    if not answer_text:
        return "Thanks — that helps me understand your setup better."
    if len(answer_text) > 100 or "\n" in answer_text:
        return "Thanks — that helps me understand your setup better."

    answer_norm = _norm(answer_text)
    if "challenge" in question or "problem" in question:
        return f"Got it — {answer_text} sounds like the main challenge right now."
    if (
        ("manage" in question and "lead" in question)
        or "lead-management" in question
        or "lead management" in question
    ):
        return f"Understood — you're currently managing leads through {answer_text}."
    if (
        ("how many" in question and ("lead" in question or "enquir" in question))
        or ("lead" in question and ("per day" in question or "daily" in question))
    ):
        return f"Thanks — {answer_text} gives me a clear picture of your daily lead volume."
    if "run ads" in question or "running ads" in question:
        if answer_norm == "yes":
            return "Got it — you're currently running ads."
        if answer_norm == "no":
            return "Understood — you're not currently running ads."
        return "Got it — that helps me understand your current ad activity."
    if ("lead" in question and "come from" in question) or "lead source" in question:
        label = "sources" if any(token in answer_text for token in (";", ",", " and ")) else "source"
        return f"Thanks — I've noted {answer_text} as your main lead {label}."
    if "type of business" in question or "industry" in question:
        return f"Got it — {answer_text} gives me useful context about your business."
    if "sales team" in question or "salespeople" in question:
        return f"Thanks — {answer_text} gives me a clear picture of your sales team size."
    if "budget" in question:
        return f"Got it — {answer_text} gives me useful budget context."
    if "main goal" in question or ("goal" in question and "shvya" in question):
        return f"That makes sense — {answer_text} gives me a clear picture of what you want to improve."
    if ("crm" in question or "lead-management software" in question) and (
        answer_norm in {"yes", "no"}
    ):
        return "Thanks — that clarifies your current CRM setup."
    if "how soon" in question or "implement" in question:
        return f"Understood — {answer_text} is your implementation timeline."
    if "purchase decision" in question or "final purchase" in question:
        return "Thanks — that clarifies how the purchase decision works on your side."
    if "whatsapp setup" in question:
        return f"Got it — {answer_text} is your current WhatsApp setup."
    if "customer conversations" in question and (
        "month" in question or "monthly" in question
    ):
        return f"Thanks — {answer_text} gives me a clear picture of your monthly conversation volume."
    if answer_norm in {"yes", "no"}:
        return "Got it — thanks for confirming that."
    return f"Thanks — I've noted {answer_text}. That gives me useful context."


def _ack_from_message(message: str, plan: dict[str, Any]) -> str:
    text = str(message or "").replace("\\n", "\n").strip()
    final_value = str(
        ((plan.get("final_configured_acknowledgement") or {}).get("value") or "")
    ).strip()
    next_rendered = str(
        ((plan.get("next_requirement") or {}).get("rendered") or "")
    ).strip()
    if final_value:
        text = text.replace(final_value, " ")
    if next_rendered:
        text = text.replace(next_rendered, " ")

    next_question = _norm((plan.get("next_requirement") or {}).get("question"))
    option_values = {
        _norm(item.get("value"))
        for item in ((plan.get("next_requirement") or {}).get("options") or [])
        if isinstance(item, dict)
    }
    kept = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _ACK_LABEL.match(line)
        if match:
            line = _strip_quotes(match.group("value"))
        if _LABEL_ONLY.match(line):
            continue
        # Remove inline option lists before sentence splitting; otherwise "A."
        # becomes one fragment and its option leaks as ordinary prose.
        if next_rendered:
            line = re.split(r"(?<!\w)[A-Z][).:]\s+", line, maxsplit=1)[0].strip()
        if re.search(r"\b(?:do not tell the lead|send the qualification completion|internal CRM|their AI score|notes\s*:|rules\s*:)\b", line, re.I):
            break
        normalized_line = _norm(line)
        if next_rendered and re.match(r"^[a-z0-9][).:\-]\s+", normalized_line):
            continue

        # During a backend-owned qualification turn the model contributes only
        # the acknowledgement. Any model-authored/paraphrased question or option
        # is discarded; the exact configured requirement is appended below.
        fragments = re.split(r"(?<=[.!?])\s+", line)
        for fragment in fragments:
            fragment = fragment.strip()
            if not fragment:
                continue
            normalized = _norm(fragment)
            if next_question and normalized == next_question:
                continue
            if next_rendered and (
                "?" in fragment
                or _QUESTION_FRAGMENT_RE.match(fragment)
                or re.match(r"^[a-z0-9][).:\-]\s+", normalized)
            ):
                continue
            if (
                option_values
                and re.match(r"^[a-z0-9][).:\-]\s+", normalized)
                and any(normalized.endswith(value) for value in option_values)
            ):
                continue
            kept.append(fragment)
    acknowledgement = " ".join(kept).strip()
    return (
        ""
        if _norm(acknowledgement).strip(" .!?") in _GENERIC_ACKS
        else acknowledgement
    )
