"""Validate the active production deployment contract.

Staging automation is intentionally disabled. These tests therefore exercise
only the production workflow and assert that the removed staging workflow and
its push trigger do not return accidentally.
"""

from pathlib import Path
import re
import shlex
from textwrap import dedent

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = [
    ("deploy.yml", "docker compose", "main"),
]


def _script(filename):
    workflow = (ROOT / ".github/workflows" / filename).read_text(encoding="utf-8")
    return dedent(workflow.split("          script: |\n", 1)[1])


def _worker_services(script, compose):
    prefix = f"{compose} stop --timeout 300 "
    for line in script.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(prefix):
            services = stripped[len(prefix):].strip()
            if services:
                return services
    raise AssertionError(f"{compose} worker drain command is missing")


def _running_services(script, compose):
    return f"web ws {_worker_services(script, compose)} beat"


def _application_services(script, compose):
    return {"web", "ws", "beat", *_worker_services(script, compose).split()}


@pytest.mark.parametrize(("filename", "compose", "branch"), WORKFLOWS)
def test_application_drain_precedes_schema_change(filename, compose, branch):
    script = _script(filename)
    readiness = script.index(f"{compose} up -d --wait db redis")
    worker_services = _worker_services(script, compose)
    running_services = _running_services(script, compose)
    conditional = script.index('if [ "$MIGRATION_FILES_CHANGED" -eq 1 ]; then')
    producers = script.index(f"{compose} stop --timeout 60 beat web ws")
    workers = script.index(f"{compose} stop --timeout 300 {worker_services}")
    stopped_guard = script.index(
        f"{compose} ps --status running -q {running_services}"
    )
    migrate = script.index(
        f"{compose} run --rm --no-deps web python manage.py migrate --noinput"
    )
    collectstatic = script.index(
        f"{compose} run --rm --no-deps web python manage.py collectstatic --noinput"
    )
    restart = script.index(f"{compose} up -d --no-deps {running_services}")

    assert (
        readiness
        < conditional
        < producers
        < workers
        < stopped_guard
        < migrate
        < collectstatic
        < restart
    )
    assert "pg_dump" not in script
    assert "Refusing migrations while an old" in script[stopped_guard:migrate]
    assert "exit 1" in script[stopped_guard:migrate]


@pytest.mark.parametrize(("filename", "compose", "branch"), WORKFLOWS)
def test_drain_stops_only_application_services_with_bounded_timeouts(
    filename,
    compose,
    branch,
):
    script = _script(filename)
    stopped = set()
    for line in script.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith(f"{compose} stop "):
            continue
        arguments = shlex.split(stripped[len(compose) :])
        assert arguments[:2] == ["stop", "--timeout"]
        assert 0 < int(arguments[2]) <= 300
        stopped.update(arguments[3:])
    assert stopped == _application_services(script, compose)
    assert not stopped.intersection(
        {"db", "redis", "whatsapp-web-gateway", "nginx", "certbot"}
    )
    assert not re.search(
        r"(?m)^(?:docker compose|\$COMPOSE)\s+(?:down|kill|rm)\b",
        script,
    )


@pytest.mark.parametrize(("filename", "compose", "branch"), WORKFLOWS)
def test_failed_schema_rollout_reports_maintenance_without_restarting_old_images(
    filename,
    compose,
    branch,
):
    script = _script(filename)
    hook_start = script.index("report_maintenance_failure() {")
    hook_end = script.index("trap report_maintenance_failure EXIT", hook_start)
    failure_hook = script[hook_start:hook_end]
    assert "application services may remain stopped" in failure_hook
    assert "do not restart old images against the new schema" in failure_hook
    assert "return \"$deploy_status\"" in failure_hook
    assert f"{compose} up" not in failure_hook
    assert "${BACKUP_FILE}.gz" not in failure_hook

    maintenance = script.index("APPLICATION_MAINTENANCE=1")
    restart = script.index(
        f"{compose} up -d --no-deps {_running_services(script, compose)}"
    )
    ready = script.index("Waiting for Django/Gunicorn dependency readiness...")
    clear = script.index("APPLICATION_MAINTENANCE=0", maintenance)
    assert hook_end < maintenance < restart < ready < clear


@pytest.mark.parametrize(("filename", "compose", "branch"), WORKFLOWS)
def test_deploy_keeps_the_trigger_commit_pinned_before_any_schema_work(
    filename,
    compose,
    branch,
):
    script = _script(filename)
    assert "github.event.workflow_run.head_sha" in script
    assert f'git merge-base --is-ancestor "$DEPLOY_SHA" origin/{branch}' in script
    assert 'git reset --hard "$DEPLOY_SHA"' in script
    assert '[ "$ACTUAL_SHA" != "$DEPLOY_SHA" ]' in script
    schema_guard = script.index('if [ "$MIGRATION_FILES_CHANGED" -eq 1 ]; then')
    assert script.index('[ "$ACTUAL_SHA" != "$DEPLOY_SHA" ]') < schema_guard
    assert f"git reset --hard origin/{branch}" not in script


def test_production_success_marker_advances_only_after_public_verification():
    script = _script("deploy.yml")
    marker = script.index('STATE_FILE="/opt/shvya-ai/.last-successful-production-deploy"')
    public_check = script.index("Verifying the public HTTPS endpoint...")
    public_failure = script.index("Public HTTPS endpoint failed readiness verification.")
    record = script.index('mv "${STATE_FILE}.tmp" "$STATE_FILE"')
    prune = script.index("docker image prune -f")

    assert marker < public_check < public_failure < record < prune


def test_production_gateway_rebuild_is_change_scoped_but_health_checks_remain():
    script = _script("deploy.yml")
    diff_check = script.index(
        'git diff --quiet "$LAST_SUCCESS_SHA" "$DEPLOY_SHA" -- '
        "whatsapp_web_gateway docker-compose.yml"
    )
    conditional_build = script.index(
        'if [ "$GATEWAY_CHANGED" -eq 1 ]; then',
        diff_check,
    )
    build = script.index("docker compose build whatsapp-web-gateway", conditional_build)
    health = script.index("Waiting for the WhatsApp Web gateway health endpoint...")
    authenticated_probe = script.index(
        "Verifying Django can resolve and authenticate to the WhatsApp Web gateway..."
    )
    callback_probe = script.index(
        "Verifying the WhatsApp Web gateway can post callbacks to Django over the private network..."
    )

    assert diff_check < conditional_build < build < health < authenticated_probe < callback_probe


def test_production_deploy_requires_same_sha_security_success_before_ssh():
    workflow = (ROOT / ".github/workflows" / "deploy.yml").read_text(
        encoding="utf-8"
    )
    security_gate = workflow.index("Require Security success for deployment SHA")
    ssh_deploy = workflow.index("Deploy over SSH")

    assert "actions: read" in workflow
    assert 'actions/workflows/security.yml/runs' in workflow
    assert 'head_sha="${DEPLOY_SHA}"' in workflow
    assert '-f event=push' in workflow
    assert '-f branch=main' in workflow
    assert "completed:success" in workflow
    assert security_gate < ssh_deploy


def test_production_deploy_never_generates_credential_encryption_key():
    script = _script("deploy.yml")
    required_guard = script.index("require_env_secret CREDENTIAL_ENCRYPTION_KEY")

    assert "ensure_env_secret CREDENTIAL_ENCRYPTION_KEY" not in script
    assert "require_env_secret() {" in script
    assert "Refusing deployment instead of replacing an existing encryption root." in script
    assert required_guard < script.index("docker compose config --quiet")


def test_staging_automation_remains_disabled():
    assert not (ROOT / ".github/workflows" / "deploy-staging.yml").exists()

    workflow = (ROOT / ".github/workflows" / "security.yml").read_text(
        encoding="utf-8"
    )
    push_block = workflow.split("  push:\n", 1)[1].split(
        "  workflow_dispatch:",
        1,
    )[0]
    assert "branches: [main]" in push_block
    assert "staging" not in push_block
    assert "paths:" not in push_block


def test_production_deploy_verifies_runtime_environment_and_oauth_origin():
    script = _script("deploy.yml")
    public_check = script.index("Verifying the public HTTPS endpoint...")
    runtime_check = script.index("Verifying production Django environment identity...")
    oauth_check = script.index("Verifying public Operations OAuth metadata...")
    record = script.index('mv "${STATE_FILE}.tmp" "$STATE_FILE"')

    assert "settings.APP_ENV == 'production'" in script
    assert (
        "settings.OPERATIONS_PUBLIC_BASE_URL == "
        "'https://dashboard.shvya-ai.com'"
    ) in script
    assert (
        "https://dashboard.shvya-ai.com/"
        ".well-known/oauth-authorization-server/operations"
    ) in script
    assert "data.get('issuer') == base + '/operations'" in script
    assert public_check < runtime_check < oauth_check < record


@pytest.mark.parametrize(("filename", "compose", "branch"), WORKFLOWS)
def test_application_image_is_built_once_and_shared_before_rollout(
    filename,
    compose,
    branch,
):
    import yaml

    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))[
        "services"
    ]
    script = _script(filename)
    application = _application_services(script, compose)
    image = "shvya-ai-app:latest"
    assert {
        name for name, service in services.items() if service.get("image") == image
    } == application
    assert all(services[name]["build"] == "." for name in application)
    builds = [
        shlex.split(line.strip()[len(compose) :])
        for line in script.splitlines()
        if line.strip().startswith(f"{compose} build ")
    ]
    assert builds == [["build", "web"], ["build", "whatsapp-web-gateway"]]
    assert script.index(f"{compose} build web\n") < script.index(
        f"{compose} run --rm --no-deps"
    )
