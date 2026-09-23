#!/usr/bin/env python3
"""Reproducible service-level capacity probe for SHVYA.

The large named profiles are intentionally opt-in. The default smoke profile is
safe for a developer database and produces comparable JSON without making a
claim about production capacity. Run against an isolated database.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
import json
import math
import os
from pathlib import Path
import platform
import resource
import statistics
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.testing")

import django  # noqa: E402

django.setup()

from django.core.cache import cache  # noqa: E402
from django.db import close_old_connections, connection  # noqa: E402
from django.test import RequestFactory  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from django.utils import timezone  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.crm.models import Lead  # noqa: E402
from apps.crm.views import dashboard as _dashboard_bootstrap  # noqa: E402,F401
from apps.organizations.models import Organization  # noqa: E402
from services.crm.dashboard_query_service import build_lead_table_context  # noqa: E402


PROFILES_PATH = Path(__file__).with_name("capacity_profiles.json")


def percentile(values: list[float], value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0, math.ceil((value / 100) * len(ordered)) - 1)
    return ordered[position]


def summary(latencies: list[float], elapsed: float, errors: int) -> dict:
    return {
        "requests": len(latencies) + errors,
        "errors": errors,
        "error_rate": round(errors / max(1, len(latencies) + errors), 6),
        "rps": round((len(latencies) + errors) / max(elapsed, 0.000001), 3),
        "latency_ms": {
            "p50": round(percentile(latencies, 50), 3),
            "p95": round(percentile(latencies, 95), 3),
            "p99": round(percentile(latencies, 99), 3),
            "mean": round(statistics.fmean(latencies), 3) if latencies else 0.0,
        },
    }


def load_profiles() -> dict:
    return json.loads(PROFILES_PATH.read_text(encoding="utf-8"))


def ensure_fixture(profile: dict, run_id: str) -> tuple[Organization, User]:
    """Materialize the declared profile; never report uncreated tenant volume."""
    selected = None
    for org_index in range(int(profile["organizations"])):
        organization, _ = Organization.objects.get_or_create(
            name=f"Capacity Probe {run_id} {org_index:05d}",
        )
        for user_index in range(int(profile["users_per_organization"])):
            user, _ = User.objects.get_or_create(
                email=f"capacity-{run_id}-{org_index}-{user_index}@example.invalid",
                defaults={
                    "organization": organization,
                    "name": f"Capacity User {user_index}",
                    "role": User.Role.ADMIN if user_index == 0 else User.Role.AGENT,
                    "is_active": True,
                },
            )
            if user.organization_id != organization.id:
                raise RuntimeError("Capacity fixture user belongs to another organization.")
            if selected is None:
                selected = (organization, user)

        pipeline = (
            organization.pipelines.filter(is_active=True).order_by("created_at").first()
        )
        if pipeline is None:
            raise RuntimeError("Organization bootstrap did not create a pipeline.")
        stage = pipeline.stages.filter(is_active=True).order_by("display_order").first()
        if stage is None:
            raise RuntimeError("Organization bootstrap did not create a stage.")

        target = int(profile["leads_per_organization"])
        existing = Lead.objects.filter(organization=organization).count()
        batch = []
        for index in range(existing, target):
            batch.append(
                Lead(
                    organization=organization,
                    pipeline=pipeline,
                    stage=stage,
                    name=f"Capacity Lead {index:09d}",
                    phone=f"+1999{index:010d}",
                    email="",
                    notes="Capacity fixture",
                    stage_entered_at=timezone.now() - timedelta(days=index % 30),
                )
            )
            if len(batch) == 2000:
                Lead.objects.bulk_create(batch, batch_size=2000)
                batch.clear()
        if batch:
            Lead.objects.bulk_create(batch, batch_size=2000)
    if selected is None:
        raise RuntimeError("Capacity profile must contain at least one organization.")
    return selected


@contextmanager
def query_count():
    with CaptureQueriesContext(connection) as captured:
        yield captured


def dashboard_probe(user: User) -> int:
    pipeline = user.organization.pipelines.filter(is_active=True).order_by("created_at").first()
    request = RequestFactory().get("/dashboard/leads/table/")
    context = build_lead_table_context(request=request, user=user, pipeline=pipeline)
    return sum(group["count"] for group in context["stage_groups"])


def search_probe(user: User) -> int:
    return len(
        list(
            Lead.objects.filter(
                organization_id=user.organization_id,
                name__icontains="Capacity Lead 0000000",
            )
            .select_related("pipeline", "stage")
            .order_by("-created_at")[:50]
        )
    )


def lead_detail_probe(user: User) -> str:
    lead = (
        Lead.objects.filter(organization_id=user.organization_id)
        .select_related("pipeline", "stage")
        .order_by("-created_at")
        .first()
    )
    return str(lead.id) if lead else ""


SCENARIOS = {
    "crm_dashboard": dashboard_probe,
    "crm_lead_search": search_probe,
    "crm_lead_detail": lead_detail_probe,
}


def run_scenario(name: str, func, user_id, iterations: int, concurrency: int) -> dict:
    errors = 0
    latencies = []

    def execute(_):
        close_old_connections()
        started = time.perf_counter()
        try:
            user = User.objects.select_related("organization").get(pk=user_id)
            func(user)
            return (time.perf_counter() - started) * 1000, None
        except Exception as exc:  # pragma: no cover - operational reporting path
            return 0.0, f"{type(exc).__name__}: {exc}"
        finally:
            close_old_connections()

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        for latency, error in executor.map(execute, range(iterations)):
            if error:
                errors += 1
            else:
                latencies.append(latency)
    elapsed = time.perf_counter() - started
    return summary(latencies, elapsed, errors)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="smoke")
    parser.add_argument("--run-id", default="local")
    parser.add_argument("--output")
    parser.add_argument("--allow-large", action="store_true")
    args = parser.parse_args()

    profiles = load_profiles()
    if args.profile not in profiles:
        parser.error(f"Unknown profile {args.profile!r}; choose from {', '.join(profiles)}")
    if args.profile != "smoke" and not args.allow_large:
        parser.error("Named capacity profiles require --allow-large and an isolated environment.")

    profile = profiles[args.profile]
    run_id = "".join(ch for ch in args.run_id if ch.isalnum() or ch in "-_")[:32]
    if not run_id:
        parser.error("--run-id must contain a letter or number")

    organization, user = ensure_fixture(profile, run_id)
    cache.clear()

    query_counts = {}
    for name, func in SCENARIOS.items():
        with query_count() as captured:
            func(user)
        query_counts[name] = len(captured)

    redis_samples = []
    for _ in range(20):
        started = time.perf_counter()
        cache.set(f"capacity-probe:{uuid.uuid4()}", "1", timeout=5)
        redis_samples.append((time.perf_counter() - started) * 1000)

    results = {}
    for name, func in SCENARIOS.items():
        results[name] = run_scenario(
            name,
            func,
            user.id,
            int(profile["iterations"]),
            int(profile["concurrency"]),
        )

    payload = {
        "schema_version": 1,
        "captured_at": timezone.now().isoformat(),
        "git_sha": os.environ.get("GIT_SHA", "unknown"),
        "profile": args.profile,
        "profile_config": profile,
        "fixture": {
            "organization_id": str(organization.id),
            "lead_count": Lead.objects.filter(organization=organization).count(),
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "process_max_rss_bytes": _process_max_rss_bytes(),
            "database_connections": _database_connection_count(),
        },
        "database_query_counts": query_counts,
        "redis_set_latency_ms": {
            "p50": round(percentile(redis_samples, 50), 3),
            "p95": round(percentile(redis_samples, 95), 3),
            "p99": round(percentile(redis_samples, 99), 3),
        },
        "scenarios": results,
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    return 0


def _database_connection_count():
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
            )
            return int(cursor.fetchone()[0])
    except Exception:
        return None


def _process_max_rss_bytes():
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


if __name__ == "__main__":
    raise SystemExit(main())
