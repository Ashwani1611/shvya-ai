from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location("ci_change_plan", ROOT / "scripts" / "ci_change_plan.py")
MODULE = module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)
classify = MODULE.classify


def test_docs_only_is_fast():
    plan = classify(["README.md", "docs/architecture.md"])
    assert plan["docs_only"] == "true"
    assert plan["full"] == "false"
    assert plan["targeted"] == "false"


def test_runtime_mcp_markdown_is_tested_and_built():
    plan = classify(["apps/integrations/operations/setup_assets/prompts/ai-playbook.template.md"])
    assert plan["docs_only"] == "false"
    assert plan["django"] == "true"
    assert plan["docker_app"] == "true"
    assert plan["targeted"] == "true"
    assert "apps/integrations/tests" in plan["pytest_targets"]
    assert "apps/ai_engagement/tests" in plan["pytest_targets"]


def test_crm_change_routes_to_related_tests_without_full_suite():
    plan = classify(["apps/crm/views/lead.py"])
    assert plan["full"] == "false"
    assert plan["django"] == "true"
    assert plan["targeted"] == "true"
    assert "apps/crm/tests" in plan["pytest_targets"]
    assert "apps/triggers/tests" in plan["pytest_targets"]
    assert "apps/followups/tests" in plan["pytest_targets"]


def test_model_change_checks_migrations_but_stays_targeted_until_migration_exists():
    plan = classify(["apps/crm/models/reminder.py"])
    assert plan["full"] == "false"
    assert plan["migration_check"] == "true"
    assert plan["targeted"] == "true"


def test_migration_change_uses_full_safety_path():
    plan = classify(["apps/crm/migrations/0028_example.py"])
    assert plan["full"] == "true"
    assert plan["migration_check"] == "true"
    assert plan["docker_app"] == "true"
    assert plan["gateway"] == "true"
    assert plan["browser"] == "true"


def test_hosted_template_runs_channels_and_real_browser_test():
    plan = classify(["templates/channels/hosted_whatsapp_chats.html"])
    assert plan["full"] == "false"
    assert plan["targeted"] == "true"
    assert plan["browser"] == "true"
    assert "apps/channels/tests" in plan["pytest_targets"]
    assert plan["browser_targets"] == "tests/browser/test_hosted_chat_browser.py"


def test_support_change_routes_browser_regressions():
    plan = classify(["apps/support/views.py"])
    assert plan["full"] == "false"
    assert plan["browser"] == "true"
    assert "apps/support/tests/test_attention_browser.py" in plan["browser_targets"]
    assert "apps/support/tests/test_csrf.py" in plan["browser_targets"]


def test_gateway_only_change_does_not_start_django_suite():
    plan = classify(["whatsapp_web_gateway/src/index.js"])
    assert plan["gateway"] == "true"
    assert plan["full"] == "false"
    assert plan["django"] == "false"
    assert plan["targeted"] == "false"


def test_compose_change_validates_and_rebuilds_both_images():
    plan = classify(["docker-compose.yml"])
    assert plan["compose"] == "true"
    assert plan["docker_app"] == "true"
    assert plan["gateway"] == "true"
    assert plan["full"] == "false"


def test_ci_self_change_forces_every_safety_gate():
    plan = classify([".github/workflows/ci.yml"])
    assert plan["full"] == "true"
    for key in (
        "python",
        "django",
        "migration_check",
        "ai",
        "gateway",
        "browser",
        "docker_app",
        "compose",
    ):
        assert plan[key] == "true"


def test_cross_cutting_service_change_falls_back_to_full_suite():
    plan = classify(["services/followup_service.py"])
    assert plan["full"] == "true"


def test_static_frontend_change_avoids_unrelated_backend_work():
    plan = classify(["frontend/pricing.html"])
    assert plan["full"] == "false"
    assert plan["django"] == "false"
    assert plan["targeted"] == "false"
    assert plan["docker_app"] == "false"


def test_manual_or_untrusted_diff_forces_full_suite():
    plan = classify([], force_full=True)
    assert plan["full"] == "true"
    assert plan["compose"] == "true"
    assert plan["browser"] == "true"
