"""Run recorded AI regressions only against a disposable pytest database."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from xml.etree import ElementTree

from .scenarios import load_scenarios

ROOT = Path(__file__).resolve().parents[3]
TEST_MODULES = (
    "test_ai_evaluation_scenarios.py", "test_grounding_safety_contract.py",
    "test_action_receipts.py", "test_sales_intelligence.py", "test_ai_phase_integration.py",
    "test_tenant_runtime_phase4.py", "test_intent_engine_phase2.py",
    "test_conversation_policy_phase3.py", "test_phase7_composite_policy.py",
    "test_qualification_execution_contract_e2e.py", "test_engagement_recovery.py",
    "test_live_whatsapp_pipeline_regressions.py",
)
CATEGORIES = ("hallucination", "qualification", "rag", "actions", "tenant_isolation", "language", "intent", "silence", "behaviour")


def summarize_junit(path, *, returncode):
    report = {"mode": "recorded_provider_backend_regression", "passed": 0, "failed": 0,
              "skipped": 0, "failures_by_category": dict.fromkeys(CATEGORIES, 0), "failures": [],
              "live_model_evaluated": False, "customer_messages_sent": False, "returncode": returncode}
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        report["failed"] = 1
        report["failures_by_category"]["behaviour"] = 1
        report["failures"] = [{"test": "evaluation_runner", "reason": "Missing or invalid test report"}]
        return report
    for case in root.iter("testcase"):
        failure = case.find("failure")
        if failure is None:
            failure = case.find("error")
        if failure is None and case.find("skipped") is not None:
            report["skipped"] += 1
        elif failure is None:
            report["passed"] += 1
        else:
            report["failed"] += 1
            text = (failure.get("message", "") + " " + (failure.text or ""))
            match = re.search(r"\[(" + "|".join(CATEGORIES) + r")\]", text)
            props = {p.get("name"): p.get("value") for p in case.findall("./properties/property")}
            name = str(case.get("classname", "")) + "::" + str(case.get("name", ""))
            category = match.group(1) if match else props.get("evaluation_category", "")
            if category not in CATEGORIES:
                category = next((value for needle, value in (
                    ("grounding", "hallucination"), ("tenant", "tenant_isolation"),
                    ("qualification", "qualification"), ("receipt", "actions"),
                    ("retrieval", "rag"), ("intent", "intent")) if needle in name), "behaviour")
            report["failures_by_category"][category] += 1
            report["failures"].append({"test": name, "category": category, "reason": failure.get("message", "")[:1600]})
    if returncode != 0 and not report["failed"]:
        report["failed"] = 1
        report["failures_by_category"]["behaviour"] += 1
        report["failures"].append({"test": "evaluation_runner", "reason": f"pytest exit status {returncode}"})
    if not report["passed"] and not report["failed"]:
        report["failed"] = 1
        report["failures_by_category"]["behaviour"] += 1
        report["failures"].append({"test": "evaluation_runner", "reason": "No passing tests were executed"})
    return report


def run_evaluation(*, scenario_path=None, output=None, settings_module="config.settings.testing"):
    load_scenarios(scenario_path)  # Fail before spawning a test process on invalid input.
    environment = os.environ.copy()
    environment.pop("SHVYA_AI_EVALUATION_SCENARIOS", None)
    if scenario_path:
        environment["SHVYA_AI_EVALUATION_SCENARIOS"] = str(Path(scenario_path).resolve())
    with tempfile.TemporaryDirectory(prefix="shvya-ai-evaluation-") as temporary:
        junit = Path(temporary) / "junit.xml"
        command = [sys.executable, "-m", "pytest", "--ds=" + settings_module,
                   "--no-cov", "-q", "--override-ini=junit_family=legacy", "--junitxml=" + str(junit)]
        command.extend("apps/ai_engagement/tests/" + module for module in TEST_MODULES)
        try:
            result = subprocess.run(command, cwd=ROOT, env=environment, timeout=900, check=False)
            code = result.returncode
        except subprocess.TimeoutExpired:
            code = 124
        report = summarize_junit(junit, returncode=code)
    if output:
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
