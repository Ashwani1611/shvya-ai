"""Operator-only, no-send validation of the existing AI Sandbox runtime.

This is not a second answer engine or a production activation mechanism. Provider
and embedding calls still use SHVYA's credit-metered adapters. Default preflight
performs only database/configuration reads; live replay is explicitly requested
by the management command. Reports deliberately exclude prompts and replies.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from copy import deepcopy
from time import monotonic, perf_counter
from uuid import uuid4

from apps.ai_engagement.services.recovery_scenarios import (
    RecoveryEvaluationError, check_preview, validate_scenarios,
)


MAX_SNAPSHOT_ROWS = 5000
MODEL_SETTINGS = (
    "OPENAI_AI_MODEL", "OPENAI_ENGAGEMENT_MODEL", "OPENAI_QUALIFICATION_MODEL",
    "OPENAI_EMBEDDING_MODEL", "OPENAI_ENGAGEMENT_MAX_OUTPUT_TOKENS",
    "OPENAI_TIMEOUT_SECONDS", "AI_RAG_MIN_SIMILARITY", "AI_BRAIN_RECOVERY_BUDGET_SECONDS",
)


def model_configuration() -> dict:
    from django.conf import settings
    return {key: str(os.getenv(key) or getattr(settings, key, "") or "")
            for key in MODEL_SETTINGS}


def preflight(organization) -> dict:
    """Read this process's flags and tenant metadata, without testing providers."""
    from django.conf import settings
    from django.db.models import Exists, OuterRef
    from apps.ai_engagement.models import Chunk, Document, FAQ, OrgInfo
    from apps.ai_engagement.services.evidence_recovery import enabled

    info = OrgInfo.objects.filter(organization=organization).first()
    all_documents = Document.objects.filter(organization=organization)
    documents = all_documents.filter(is_active=True)
    chunks = Chunk.objects.filter(
        organization=organization, is_active=True, document__organization=organization,
        document__is_active=True, document__processing_status="completed",
    )
    missing_chunks = documents.filter(processing_status="completed").annotate(
        has_chunks=Exists(chunks.filter(document_id=OuterRef("pk"))),
    ).filter(has_chunks=False)
    counts = {"active_documents": documents.count(), "searchable_chunks": chunks.count(),
              "embedded_chunks": chunks.filter(embedding__isnull=False).count(),
              "active_faqs": FAQ.objects.filter(organization=organization, is_active=True).count(),
              "failed_documents": all_documents.filter(processing_status="failed").count(),
              "unready_documents": all_documents.exclude(processing_status__in=["completed", "failed"]).count(),
              "completed_documents_without_chunks": missing_chunks.count(),
              "guided_files_marked_ready": documents.filter(file_sharing_ready=True).count()}
    return {
        "mode": "read_only_preflight", "organization_id": str(organization.pk),
        "global_recovery_enabled": os.getenv("AI_BRAIN_RECOVERY_ENABLED", "0") == "1",
        "organization_recovery_enabled": enabled({"organization": organization}),
        "organization_ai_configured": info is not None,
        "organization_ai_enabled": bool(info and info.ai_enabled),
        "about_configured": bool(info and str(info.about or "").strip()),
        "playbook_configured": bool(info and str(info.ai_playbook or "").strip()),
        "languages_configured": bool(info and str(info.bot_languages or "").strip()),
        "provider_key_configured": bool(getattr(settings, "OPENAI_API_KEY", "")),
        "model_configuration": model_configuration(), "source_counts": counts,
        "unindexed_document_ids": list(missing_chunks.order_by("pk").values_list("pk", flat=True)[:20]),
        "live_model_evaluated": False, "customer_messages_sent": False,
        "runtime_scope": "current_process_only",
        "file_bytes_accessibility_checked": False,
        "activation_changed": False,
        "repair_document_ids": list(all_documents.filter(processing_status="failed")
            .order_by("-updated_at", "pk").values_list("pk", flat=True)[:20]),
        "repair_command": "repair_ai_knowledge; dry-run first, --apply requires an exact fingerprint",
    }


def snapshot_fingerprint(organization) -> str:
    """Hash bounded source/config rows without returning content or credentials.

    Includes embeddings and actual text, not merely updated_at: queryset.update
    need not advance timestamps. This detects drift, not a cross-turn database
    snapshot lock; use a quiescent internal organization for valid comparisons.
    """
    from django.core.serializers.json import DjangoJSONEncoder
    from apps.ai_engagement.models import Chunk, Document, FAQ, OrgInfo
    from apps.crm.models import AttributeDefinition, Pipeline, Stage

    class Encoder(DjangoJSONEncoder):
        def default(self, value):
            if hasattr(value, "tolist"):
                return value.tolist()
            return super().default(value)

    organization.refresh_from_db()
    digest = hashlib.sha256()

    def feed(value):
        digest.update(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                 cls=Encoder, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")

    feed({"organization_id": str(organization.pk), "name": organization.name,
          "settings": organization.settings, "models": model_configuration()})
    queries = (
        ("info", OrgInfo.objects.filter(organization=organization)),
        ("faq", FAQ.objects.filter(organization=organization, is_active=True)),
        ("document", Document.objects.filter(organization=organization, is_active=True)),
        ("chunk", Chunk.objects.filter(organization=organization, is_active=True,
            document__organization=organization, document__is_active=True)),
        ("pipeline", Pipeline.objects.filter(organization=organization, is_active=True)),
        ("stage", Stage.objects.filter(pipeline__organization=organization,
            pipeline__is_active=True, is_active=True)),
        ("attribute", AttributeDefinition.objects.filter(organization=organization, is_active=True)),
    )
    total = 0
    for name, query in queries:
        feed(name)
        for row in query.order_by("pk").values().iterator(chunk_size=50):
            total += 1
            if total > MAX_SNAPSHOT_ROWS:
                raise RecoveryEvaluationError("snapshot_row_limit_exceeded")
            feed(row)
    return digest.hexdigest()


def _memory_playground():
    from apps.ai_engagement.services.playground import PlaygroundService

    class MemoryPlayground(PlaygroundService):
        def __init__(self):
            super().__init__()
            self.sessions = {}

        def _load_session_payload(self, *, organization, session_id):
            return deepcopy(self.sessions.get((str(organization.pk), session_id), {}))

        def _save_history(self, *, organization, session_id, **payload):
            self.sessions[(str(organization.pk), session_id)] = deepcopy(payload)

        def attributes_for(self, *, organization, session_id):
            return self._load_session_payload(organization=organization, session_id=session_id).get("attributes", {})

    return MemoryPlayground()


def _safe_recovery_events(buffer) -> list[dict]:
    events = (buffer.data.get("knowledge_recovery") or {}).get("events") or []
    safe_keys = {"step", "status", "embedding_status", "attempt", "candidate_count",
                 "retained_count", "part_count", "supported_count"}
    return [{key: value for key, value in item.items() if key in safe_keys}
            for item in events[-12:] if isinstance(item, dict)]


def evaluate(organization, payload: dict, *, max_turns=8, budget_seconds=120.0) -> dict:
    """Run OFF/ON variants with private in-memory sessions; never activate tenants.

    This function is an explicit live-model entry point for the CLI, not an HTTP
    endpoint. It intentionally reuses PlaygroundService, not EngagementService
    with a real CRM lead. max_turns counts both variants; it is not a token cap.
    """
    from apps.ai_engagement.services.evidence_recovery import sandbox_recovery_preview
    from apps.ai_engagement.services.trace_service import TraceBuffer, _CURRENT
    from apps.ai_engagement.services.usage_observation import observe_usage, usage_report, summarize_usage
    from apps.crm.models import Lead, Stage

    cases = validate_scenarios(payload)
    total_turns = sum(len(case["turns"]) for case in cases) * 2
    if type(max_turns) is not int or not 2 <= max_turns <= 40 or total_turns > max_turns:
        raise RecoveryEvaluationError("comparison_turn_budget_exceeded")
    if (isinstance(budget_seconds, bool) or not isinstance(budget_seconds, (int, float))
            or not math.isfinite(budget_seconds) or not 10 <= budget_seconds <= 600):
        raise RecoveryEvaluationError("invalid_comparison_time_budget")
    source_choices = dict(Lead._meta.get_field("lead_source").choices)
    for case in cases:
        if "lead_source" in case and case["lead_source"] not in source_choices:
            raise RecoveryEvaluationError("invalid_lead_source")
        if case.get("stage_id") and not Stage.objects.filter(pk=case["stage_id"],
                pipeline__organization=organization, pipeline__is_active=True, is_active=True).exists():
            raise RecoveryEvaluationError("stage_not_in_active_organization")
    readiness = preflight(organization)
    if not readiness["organization_ai_configured"]:
        raise RecoveryEvaluationError("organization_ai_not_configured")
    if not readiness["organization_ai_enabled"] or not getattr(organization, "is_active", True):
        raise RecoveryEvaluationError("organization_ai_disabled")
    if not readiness["provider_key_configured"]:
        raise RecoveryEvaluationError("provider_not_configured")

    started = monotonic()
    fingerprint = snapshot_fingerprint(organization)
    report = {"mode": "live_model_sandbox_comparison", "organization_id": str(organization.pk),
              "source_fingerprint": fingerprint, "source_snapshot_unchanged": True,
              "model_configuration": readiness["model_configuration"], "cases": [],
              "completed_turns": 0, "live_model_evaluated": False,
              "customer_messages_sent": False, "delivery_verified": False,
              "activation_changed": False, "comparison_valid": True,
              "requires_human_review": True, "semantic_accuracy_verified": False,
              "recovery_exercised": False, "credit_usage_measured": False}

    def boundary():
        if monotonic() - started >= budget_seconds:
            raise RecoveryEvaluationError("comparison_time_budget_exceeded")
        if snapshot_fingerprint(organization) != fingerprint:
            report["source_snapshot_unchanged"] = False
            raise RecoveryEvaluationError("source_changed_during_comparison")

    try:
        for case in cases:
            entry = {"id": case["id"], "channel": case["channel"], "baseline": [], "recovery": []}
            report["cases"].append(entry)
            for variant in ("baseline", "recovery"):
                runner = _memory_playground()
                session_id = f"recovery-eval-{uuid4().hex}"
                for number, turn in enumerate(case["turns"], 1):
                    boundary()
                    buffer = TraceBuffer(trace_id=None, organization_id=str(organization.pk),
                                         started_perf=perf_counter())
                    token = _CURRENT.set(buffer)
                    turn_started = monotonic()
                    try:
                        with observe_usage(organization.pk) as usage, sandbox_recovery_preview(
                                organization_id=organization.pk, use_recovery=variant == "recovery"):
                            result = runner.run(organization=organization, session_id=session_id,
                                message=turn["message"], channel=case["channel"],
                                lead_source=case.get("lead_source"), stage_id=case.get("stage_id"))
                    except Exception:
                        entry[variant].append({"turn": number, "model": "", "check_count": 0,
                            "failed_checks": 0, "completed": False, "usage": usage_report(usage),
                            "latency_ms": max(0, int((monotonic() - turn_started) * 1000)),
                            "recovery_events": _safe_recovery_events(buffer), "execution_mode": "sandbox_preview"})
                        raise
                    finally:
                        _CURRENT.reset(token)
                    checks = check_preview(result=result, expected=turn.get("expect", {}),
                        attributes=runner.attributes_for(organization=organization, session_id=session_id))
                    model = str(result.model or "")[:100]
                    events = _safe_recovery_events(buffer)
                    if variant == "recovery" and events:
                        report["recovery_exercised"] = True
                    entry[variant].append({"turn": number, "model": model, **checks,
                        "latency_ms": max(0, int((monotonic() - turn_started) * 1000)),
                        "recovery_events": events, "usage": usage_report(usage),
                        "execution_mode": "sandbox_preview"})
                    report["completed_turns"] += 1
                    # A fallback/deterministic result is not evidence that a live
                    # model was evaluated successfully on this conversation.
                    report["live_model_evaluated"] |= bool(model and model not in {"deterministic", ""}
                        and "fallback" not in model.casefold() and "failsoft" not in model.casefold())
                    boundary()
            entry["model_labels_match"] = ([x["model"] for x in entry["baseline"]]
                                            == [x["model"] for x in entry["recovery"]])
            # The configured provider/model choices are covered by the source
            # fingerprint. A fallback -> model response is precisely a recovery
            # outcome to compare, not configuration drift. Keep response labels
            # as a diagnostic; human review remains required for attribution.
    except RecoveryEvaluationError as exc:
        report.update(comparison_valid=False, stopped_reason=str(exc))
    except Exception as exc:
        # Do not copy provider exceptions, prompts, document text or credentials.
        report.update(comparison_valid=False, stopped_reason="comparison_runtime_error",
                      error_type=type(exc).__name__)
    report["acceptance"] = {}
    for variant in ("baseline", "recovery"):
        rows = [turn for case in report["cases"] for turn in case[variant]]
        checks = sum(turn["check_count"] for turn in rows)
        failed = sum(turn["failed_checks"] for turn in rows)
        report["acceptance"][variant] = {"check_count": checks, "failed_checks": failed,
            "passed": (failed == 0) if checks else None,
            "unscored_turns": sum(turn["check_count"] == 0 for turn in rows)}
    report["usage"] = summarize_usage(report["cases"], comparison_valid=report["comparison_valid"])
    report["credit_usage_measured"] = bool(report["usage"]["incremental_complete"] and any(
        turn.get("usage", {}).get("observed_reservations", 0)
        for case in report["cases"] for variant in ("baseline", "recovery") for turn in case[variant]))
    report["elapsed_ms"] = max(0, int((monotonic() - started) * 1000))
    return report
