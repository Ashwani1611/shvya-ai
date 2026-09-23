#!/usr/bin/env python3
"""Compare two capacity JSON files without inventing pass/fail thresholds."""

import argparse
import json
from pathlib import Path


def change(before, after):
    if not before:
        return None
    return round(((after - before) / before) * 100, 3)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("before")
    parser.add_argument("after")
    parser.add_argument("--output")
    args = parser.parse_args()
    before = json.loads(Path(args.before).read_text(encoding="utf-8"))
    after = json.loads(Path(args.after).read_text(encoding="utf-8"))
    comparison = {"before": args.before, "after": args.after, "scenarios": {}}
    for name in sorted(set(before["scenarios"]) & set(after["scenarios"])):
        old = before["scenarios"][name]
        new = after["scenarios"][name]
        comparison["scenarios"][name] = {
            "error_rate_before": old["error_rate"],
            "error_rate_after": new["error_rate"],
            "rps_change_percent": change(old["rps"], new["rps"]),
            "p50_change_percent": change(
                old["latency_ms"]["p50"], new["latency_ms"]["p50"]
            ),
            "p95_change_percent": change(
                old["latency_ms"]["p95"], new["latency_ms"]["p95"]
            ),
            "p99_change_percent": change(
                old["latency_ms"]["p99"], new["latency_ms"]["p99"]
            ),
        }
    rendered = json.dumps(comparison, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
