"""Build a conservative, change-aware CI execution plan.

Unknown or cross-cutting changes intentionally fall back to the full CI path.
The planner uses only the Python standard library so the first CI job does not
need to install project dependencies.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


APP_DEPENDENCIES = {
    "accounts": ("accounts",),
    "ai_engagement": ("ai_engagement", "channels"),
    "analytics": ("analytics",),
    "calls": ("calls", "telephony"),
    "channels": ("channels", "crm", "ai_engagement", "hosted_automation"),
    "copilot": ("copilot", "ai_engagement"),
    "core": ("core",),
    "crm": ("crm", "triggers", "followups"),
    "followups": ("followups", "crm"),
    "hosted_automation": ("hosted_automation", "channels"),
    "integrations": ("integrations", "channels"),
    "organizations": ("organizations",),
    "superadmin": ("superadmin", "organizations"),
    "support": ("support",),
    "teams": ("teams",),
    "telephony": ("telephony", "calls"),
    "triggers": ("triggers", "crm"),
}

AI_APPS = {"ai_engagement", "copilot", "hosted_automation", "triggers"}
FULL_SHARED_APPS = {"accounts", "core", "organizations"}
DOC_NAMES = {"LICENSE", "LICENSE.txt"}


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _docs_only_path(path: str) -> bool:
    return (
        path.startswith("docs/")
        or path.endswith(".md")
        or path in DOC_NAMES
    )


def _add_app_targets(targets: set[str], app: str) -> None:
    for dependency in APP_DEPENDENCIES.get(app, (app,)):
        targets.add(f"apps/{dependency}/tests")


def classify(paths: list[str], *, force_full: bool = False) -> dict[str, str]:
    changed = sorted({path.strip().replace("\\", "/") for path in paths if path.strip()})
    targets: set[str] = set()
    browser_targets: set[str] = set()

    plan = {
        "docs_only": False,
        "full": force_full,
        "python": False,
        "django": False,
        "migration_check": False,
        "ai": False,
        "gateway": False,
        "browser": False,
        "docker_app": False,
        "compose": False,
    }

    if changed and all(_docs_only_path(path) for path in changed) and not force_full:
        plan["docs_only"] = True
        return {
            **{key: _bool(value) for key, value in plan.items()},
            "targeted": "false",
            "pytest_targets": "",
            "browser_targets": "",
            "changed_count": str(len(changed)),
        }

    for path in changed:
        suffix = Path(path).suffix
        if suffix == ".py":
            plan["python"] = True

        if path.startswith("whatsapp_web_gateway/"):
            plan["gateway"] = True
            if path.endswith("Dockerfile") or Path(path).name in {"package.json", "package-lock.json"}:
                plan["gateway"] = True
            continue

        if path in {"docker-compose.yml", "docker-compose.staging.yml", ".env.example", ".env.staging.example"}:
            plan["compose"] = True
            plan["docker_app"] = True
            plan["gateway"] = True
            continue

        if path in {"Dockerfile", ".dockerignore"}:
            plan["docker_app"] = True
            continue

        if path in {"requirements.txt", "requirements-dev.txt", "pyproject.toml", "manage.py"}:
            plan["full"] = True
            continue

        if path in {".github/workflows/ci.yml", "scripts/ci_change_plan.py"}:
            plan["full"] = True
            continue

        if path in {".github/workflows/deploy.yml", ".github/workflows/deploy-staging.yml"}:
            targets.add("tests/test_deployment_workflows.py")
            continue

        if path.startswith(".github/workflows/"):
            # The workflow being edited has its own GitHub validation path.
            # Do not run unrelated application tests merely for YAML-only changes.
            continue

        if path.startswith(("config/", "services/", "core/")):
            plan["full"] = True
            continue

        if path.startswith("tests/"):
            # Root tests are generally cross-cutting contracts. A changed contract
            # gets the full suite rather than trying to infer its dependencies.
            plan["full"] = True
            continue

        if path.startswith("apps/"):
            parts = path.split("/")
            if len(parts) < 2 or parts[1] not in APP_DEPENDENCIES:
                plan["full"] = True
                continue

            app = parts[1]
            plan["django"] = True
            _add_app_targets(targets, app)

            if app in FULL_SHARED_APPS:
                plan["full"] = True
            if app in AI_APPS:
                plan["ai"] = True

            if "/migrations/" in path:
                plan["migration_check"] = True
                plan["full"] = True
            elif "/models/" in path or path.endswith("/models.py"):
                plan["migration_check"] = True

            if app == "support":
                plan["browser"] = True
                browser_targets.update(
                    {
                        "apps/support/tests/test_attention_browser.py",
                        "apps/support/tests/test_csrf.py",
                    }
                )

            if app == "channels" and any(token in path.lower() for token in ("hosted", "whatsapp_chat", "whatsapp/chats")):
                plan["browser"] = True
                browser_targets.add("tests/browser/test_hosted_chat_browser.py")
            continue

        if path.startswith("templates/"):
            plan["django"] = True
            parts = path.split("/")
            if len(parts) > 1 and parts[1] in APP_DEPENDENCIES:
                app = parts[1]
                _add_app_targets(targets, app)
                if app in AI_APPS:
                    plan["ai"] = True
            if path == "templates/channels/hosted_whatsapp_chats.html":
                plan["browser"] = True
                browser_targets.add("tests/browser/test_hosted_chat_browser.py")
            if len(parts) > 1 and parts[1] == "support":
                plan["browser"] = True
                browser_targets.update(
                    {
                        "apps/support/tests/test_attention_browser.py",
                        "apps/support/tests/test_csrf.py",
                    }
                )
            continue

        if path.startswith("static/"):
            parts = path.split("/")
            if len(parts) > 1 and parts[1] in APP_DEPENDENCIES:
                app = parts[1]
                _add_app_targets(targets, app)
                if app == "support":
                    plan["browser"] = True
                    browser_targets.add("apps/support/tests/test_attention_browser.py")
            continue

        if path.startswith("frontend/"):
            # Static marketing-site assets are served directly by Nginx and do
            # not require Django/database/AI/container rebuild validation.
            continue

        if path.startswith("nginx/"):
            # Deployment performs nginx -t before reload. Full Django tests add
            # no signal for an nginx-only edit.
            continue

        if path.startswith(("scripts/", "bin/")) or suffix in {".py", ".toml"}:
            plan["full"] = True
            continue

        if not _docs_only_path(path):
            # Unknown application-affecting paths take the safe route.
            plan["full"] = True

    if plan["full"]:
        plan.update(
            {
                "python": True,
                "django": True,
                "migration_check": True,
                "ai": True,
                "gateway": True,
                "browser": True,
                "docker_app": True,
                "compose": True,
            }
        )
        targets.clear()
        browser_targets.clear()

    targeted = bool(targets) and not plan["full"]
    return {
        **{key: _bool(value) for key, value in plan.items()},
        "targeted": _bool(targeted),
        "pytest_targets": " ".join(sorted(targets)),
        "browser_targets": " ".join(sorted(browser_targets)),
        "changed_count": str(len(changed)),
    }


def _git_changed_paths(base: str, head: str) -> list[str]:
    if not base or set(base) == {"0"}:
        raise ValueError("No trustworthy base SHA is available.")
    subprocess.run(
        ["git", "cat-file", "-e", f"{base}^{{commit}}"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    output = subprocess.check_output(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", base, head],
        text=True,
    )
    return output.splitlines()


def _write_outputs(path: str, plan: dict[str, str]) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in plan.items():
            handle.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--force-full", action="store_true")
    parser.add_argument("--github-output", default="")
    args = parser.parse_args()

    force_full = args.force_full
    paths: list[str] = []
    if not force_full:
        try:
            paths = _git_changed_paths(args.base, args.head)
        except (ValueError, subprocess.CalledProcessError):
            force_full = True

    plan = classify(paths, force_full=force_full)

    print("CI change plan:")
    for key, value in plan.items():
        print(f"  {key}: {value}")

    if args.github_output:
        _write_outputs(args.github_output, plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
