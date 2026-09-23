#!/usr/bin/env python3
"""Authenticated, manifest-driven HTTP load runner for staging.

The manifest intentionally contains no credentials. Supply a staging session
cookie and CSRF token through environment variables. Mutating scenarios should
target disposable fixtures and idempotency keys defined in the manifest.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import statistics
import time
from urllib import error, request


def percentile(values, percentage):
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percentage / 100) - 1)]


def render(value, variables):
    rendered = str(value)
    for name, replacement in variables.items():
        rendered = rendered.replace(f"${{{name}}}", str(replacement))
    return rendered


def execute(base_url, scenario, variables, timeout):
    path = render(scenario["path"], variables)
    body = scenario.get("json")
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Accept": "application/json,text/html",
        "User-Agent": "shvya-capacity-probe/1",
    }
    cookie = os.environ.get("SHVYA_LOAD_COOKIE", "")
    csrf = os.environ.get("SHVYA_LOAD_CSRF_TOKEN", "")
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRFToken"] = csrf
    if data is not None:
        headers["Content-Type"] = "application/json"
    started = time.perf_counter()
    status = 0
    try:
        with request.urlopen(
            request.Request(
                f"{base_url.rstrip('/')}{path}",
                data=data,
                headers=headers,
                method=scenario.get("method", "GET"),
            ),
            timeout=timeout,
        ) as response:
            status = response.status
            response.read()
    except error.HTTPError as exc:
        status = exc.code
        exc.read()
    except Exception:
        status = 0
    return (time.perf_counter() - started) * 1000, status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--manifest", default=str(Path(__file__).with_name("http_scenarios.example.json")))
    parser.add_argument("--scenario", action="append", default=[])
    parser.add_argument("--iterations", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    variables = {**manifest.get("variables", {})}
    variables.update(
        {
            key[len("SHVYA_LOAD_VAR_") :]: value
            for key, value in os.environ.items()
            if key.startswith("SHVYA_LOAD_VAR_")
        }
    )
    chosen = set(args.scenario)
    scenarios = [
        item
        for item in manifest["scenarios"]
        if not chosen or item["name"] in chosen
    ]
    if chosen - {item["name"] for item in scenarios}:
        parser.error("One or more requested scenario names are not in the manifest.")

    results = {}
    for scenario in scenarios:
        if not scenario.get("enabled", False):
            results[scenario["name"]] = {"status": "not_configured"}
            continue
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            samples = list(
                pool.map(
                    lambda _: execute(
                        args.base_url, scenario, variables, args.timeout
                    ),
                    range(args.iterations),
                )
            )
        elapsed = time.perf_counter() - started
        latencies = [latency for latency, status in samples if 200 <= status < 400]
        statuses = {}
        for _latency, status in samples:
            statuses[str(status)] = statuses.get(str(status), 0) + 1
        errors = len(samples) - len(latencies)
        results[scenario["name"]] = {
            "requests": len(samples),
            "rps": round(len(samples) / max(elapsed, 0.000001), 3),
            "error_rate": round(errors / max(1, len(samples)), 6),
            "statuses": statuses,
            "latency_ms": {
                "p50": round(percentile(latencies, 50), 3),
                "p95": round(percentile(latencies, 95), 3),
                "p99": round(percentile(latencies, 99), 3),
                "mean": round(statistics.fmean(latencies), 3) if latencies else 0,
            },
        }

    payload = {
        "schema_version": 1,
        "captured_at_epoch": time.time(),
        "base_url": args.base_url,
        "concurrency": args.concurrency,
        "iterations_per_scenario": args.iterations,
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
