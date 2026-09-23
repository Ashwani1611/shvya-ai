"""Finalize model output against backend-authoritative response plans."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from .common import (
    _ACK_LABEL,
    _GREETING_RE,
    _LABEL_ONLY,
    _QUESTION_FRAGMENT_RE,
    _norm,
    _strip_quotes,
)
from .reconciliation import _ack_from_message, _fallback_acknowledgement


def _stage_success(state: dict[str, Any]) -> bool:
    plan = state.get("response_plan") if isinstance(state, dict) else {}
    execution = plan.get("execution_results") if isinstance(plan, dict) else {}
    stage_info = execution.get("stage_transition") if isinstance(execution, dict) else {}
    result = stage_info.get("result") if isinstance(stage_info, dict) else {}
    target = (
        str(stage_info.get("target_stage_id") or "")
        if isinstance(stage_info, dict)
        else ""
    )
    stage = state.get("stage") if isinstance(state, dict) else {}
    return bool(
        stage_info
        and stage_info.get("configured")
        and target
        and isinstance(stage, dict)
        and str(stage.get("id") or "") == target
        and isinstance(result, dict)
        and result.get("verified") is True
        and _norm(result.get("status")) == "executed"
    )


def _leading_greeting(message: str) -> str:
    blocks = str(message or "").strip().split("\n\n")
    if not blocks:
        return ""
    first = blocks[0].strip()
    if not _GREETING_RE.match(first):
        return ""

    # Preserve legitimate welcome sentences, but stop before the model begins a
    # paraphrased qualification question/instruction. The backend appends the
    # exact configured requirement below.
    fragments = [
        fragment.strip()
        for fragment in re.split(r"(?<=[.!?])\s+", first)
        if fragment.strip()
    ]
    kept: list[str] = []
    for fragment in fragments:
        normalized = _norm(fragment)
        if (
            "?" in fragment
            or _QUESTION_FRAGMENT_RE.match(fragment)
            or "choose one" in normalized
            or "select one" in normalized
            or "please choose" in normalized
            or "please select" in normalized
        ):
            break
        kept.append(fragment)
    return " ".join(kept).strip()


def _finalize(decision, state):
    from apps.ai_engagement.services import canonical_architecture as canonical
    from apps.ai_engagement.services.engagement import EngagementError

    plan = state.get("response_plan") if isinstance(state, dict) else None
    if not isinstance(plan, dict):
        return decision

    kind = str(plan.get("response_type") or "")
    if kind not in {
        "qualification_start",
        "qualification_progress",
        "qualification_clarification",
        "qualification_complete",
    }:
        return decision

    rendered = str(
        ((plan.get("next_requirement") or {}).get("rendered") or "")
    ).strip()

    if kind == "qualification_start":
        if not rendered:
            raise EngagementError(
                "Qualification start plan is missing the first configured requirement."
            )
        greeting = _leading_greeting(getattr(decision, "message", ""))
        message = f"{greeting}\n\n{rendered}".strip() if greeting else rendered

    elif kind == "qualification_clarification":
        if not rendered:
            raise EngagementError(
                "Qualification clarification is missing the active configured requirement."
            )
        intro = _ack_from_message(getattr(decision, "message", ""), plan)
        message = f"{intro}\n\n{rendered}".strip() if intro else rendered

    else:
        acknowledgement = _ack_from_message(
            getattr(decision, "message", ""),
            plan,
        )
        raw_message = str(getattr(decision, "message", "") or "")
        has_internal_label = any(
            _LABEL_ONLY.match(line.strip())
            for line in raw_message.replace("\\n", "\n").splitlines()
            if line.strip()
        )
        if (
            plan.get("acknowledgement_required")
            and not acknowledgement
            and not has_internal_label
        ):
            acknowledgement = _fallback_acknowledgement(plan)
        if plan.get("acknowledgement_required") and not acknowledgement:
            raise EngagementError(
                "Qualification response requires a personalized acknowledgement."
            )

        if kind == "qualification_progress":
            if not rendered:
                raise EngagementError(
                    "Qualification progress plan is missing the next configured requirement."
                )
            message = f"{acknowledgement}\n\n{rendered}".strip()
        else:
            final_value = str(
                (
                    (plan.get("final_configured_acknowledgement") or {}).get("value")
                    or ""
                )
            ).strip()
            if not final_value:
                raise EngagementError(
                    "Qualification completion requires a configured Acknowledgment message value."
                )
            # Keep a distinct answer acknowledgment, but not a second generated
            # paraphrase of the organization's completion message.
            ack_words = set(re.findall(r"[a-z]+", acknowledgement.casefold()))
            final_words = set(re.findall(r"[a-z]+", final_value.casefold()))
            duplicate = bool(ack_words) and len(ack_words & final_words) / len(ack_words) >= 0.7
            message = final_value if duplicate else f"{acknowledgement}\n\n{final_value}".strip()

    if canonical._QUALIFIED_CLAIM_RE.search(message) and not _stage_success(state):
        message = canonical.ResponseActionValidator._replace_sentence(
            message,
            canonical._QUALIFIED_CLAIM_RE,
            "",
        )

    cleaned = []
    for raw in message.splitlines():
        line = raw.strip()
        if not line:
            cleaned.append("")
            continue
        match = _ACK_LABEL.match(line)
        if match:
            value = _strip_quotes(match.group("value"))
            if value:
                cleaned.append(value)
            continue
        if not _LABEL_ONLY.match(line):
            cleaned.append(line)
    final_message = "\n".join(cleaned).strip()
    next_id = str(
        ((plan.get("next_requirement") or {}).get("id") or "")
    ).strip() or None

    # The response-plan renderer is the final authority that actually places a
    # qualification question in the customer message. Keep decision metadata in
    # lock-step with that rendered output so the outbound post-save hook records
    # the exact question as ASKED. Without this, a language-only second pass can
    # append Q2 while leaving next_requirement_id=None, causing the customer's
    # correct Q2 answer to be rejected/repeated on the following turn.
    if kind == "qualification_complete":
        return replace(
            decision,
            message=final_message,
            next_requirement_id=None,
        )
    if kind == "qualification_clarification":
        return replace(
            decision,
            message=final_message,
            next_requirement_id=next_id,
            reason="QUALIFICATION_CLARIFY",
            reason_code="QUALIFICATION_CLARIFY",
        )
    return replace(
        decision,
        message=final_message,
        next_requirement_id=next_id,
        reason="QUALIFICATION_NEXT",
        reason_code="QUALIFICATION_NEXT",
    )
