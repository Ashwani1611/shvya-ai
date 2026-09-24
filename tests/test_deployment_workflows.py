"""Prevent old application processes from running across schema removals."""

from pathlib import Path
import re
import shlex
from textwrap import dedent

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = [
    ("deploy.yml", "docker compose", "main"),
    ("deploy-staging.yml", "$COMPOSE", "staging"),
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
    if filename == "deploy-staging.yml":
        assert f"{compose} up -d --wait db redis pgbouncer" in script
    worker_services = _worker_services(script, compose)
    running_services = _running_services(script, compose)
    producers = script.index(f"{compose} stop --timeout 60 beat web ws")
    workers = script.index(f"{compose} stop --timeout 300 {worker_services}")
    stopped_guard = script.index(f"{compose} ps --status running -q {running_services}")
    migrate = script.index(f"{compose} run --rm --no-deps web python manage.py migrate --noinput")
    collectstatic = script.index(f"{compose} run --rm --no-deps web python manage.py collectstatic --noinput")
    restart = script.index(f"{compose} up -d --no-deps {running_services}")

    if filename == "deploy.yml":
        conditional = script.index('if [ "$MIGRATION_FILES_CHANGED" -eq 1 ]; then')
        assert readiness < conditional < producers < workers < stopped_guard < migrate < collectstatic < restart
        assert "pg_dump" not in script
    else:
        backup = script.index("pg_dump")
        checked_backup = script.index('test -s "$BACKUP_FILE"')
        compressed_backup = script.index('gzip "$BACKUP_FILE"')
        assert readiness < backup < checked_backup < compressed_backup < producers < workers < stopped_guard < migrate < collectstatic < restart

    assert "Refusing migrations while an old" in script[stopped_guard:migrate]
    assert "exit 1" in script[stopped_guard:migrate]


@pytest.mark.parametrize(("filename", "compose", "branch"), WORKFLOWS)
def test_drain_stops_only_application_services_with_bounded_timeouts(filename, compose, branch):
    script = _script(filename)
    stopped = set()
    for line in script.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith(f"{compose} stop "):
            continue
        arguments = shlex.split(stripped[len(compose):])
        assert arguments[:2] == ["stop", "--timeout"]
        assert 0 < int(arguments[2]) <= 300
        stopped.update(arguments[3:])
    assert stopped == _application_services(script, compose)
    assert not stopped.intersection({"db", "redis", "whatsapp-web-gateway", "nginx", "certbot"})
    assert not re.search(r"(?m)^(?:docker compose|\$COMPOSE)\s+(?:down|kill|rm)\b", script)


@pytest.mark.parametrize(("filename", "compose", "branch"), WORKFLOWS)
def test_failed_schema_rollout_reports_maintenance_without_restarting_old_images(filename, compose, branch):
    script = _script(filename)
    hook_start = script.index("report_maintenance_failure() {")
    hook_end = script.index("trap report_maintenance_failure EXIT", hook_start)
    failure_hook = script[hook_start:hook_end]
    assert "application services may remain stopped" in failure_hook
    assert "do not restart old images against the new schema" in failure_hook
    assert "return \"$deploy_status\"" in failure_hook
    assert f"{compose} up" not in failure_hook

    if filename == "deploy.yml":
        assert "${BACKUP_FILE}.gz" not in failure_hook
        maintenance = script.index("APPLICATION_MAINTENANCE=1")
        restart = script.index(f"{compose} up -d --no-deps {_running_services(script, compose)}")
        ready = script.index("Waiting for Django/Gunicorn dependency readiness...")
        clear = script.index("APPLICATION_MAINTENANCE=0", maintenance)
        assert hook_end < maintenance < restart < ready < clear
    else:
        assert "${BACKUP_FILE}.gz" in failure_hook
        maintenance = script.index("APPLICATION_MAINTENANCE=1")
        restart = script.index(f"{compose} up -d --no-deps {_running_services(script, compose)}")
        ready = script.index(f"{compose} up -d --wait --no-deps web")
        clear = script.index("APPLICATION_MAINTENANCE=0", maintenance)
        assert hook_end < maintenance < restart < ready < clear


@pytest.mark.parametrize(("filename", "compose", "branch"), WORKFLOWS)
def test_deploy_keeps_the_trigger_commit_pinned_before_any_schema_work(filename, compose, branch):
    script = _script(filename)
    assert "github.event.workflow_run.head_sha" in script
    assert f'git merge-base --is-ancestor "$DEPLOY_SHA" origin/{branch}' in script
    assert 'git reset --hard "$DEPLOY_SHA"' in script
    assert '[ "$ACTUAL_SHA" != "$DEPLOY_SHA" ]' in script
    schema_guard = (
        script.index('if [ "$MIGRATION_FILES_CHANGED" -eq 1 ]; then')
        if filename == "deploy.yml"
        else script.index("pg_dump")
    )
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
        'git diff --quiet "$LAST_SUCCESS_SHA" "$DEPLOY_SHA" -- whatsapp_web_gateway docker-compose.yml'
    )
    conditional_build = script.index('if [ "$GATEWAY_CHANGED" -eq 1 ]; then', diff_check)
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
    workflow = (ROOT / ".github/workflows" / "deploy.yml").read_text(encoding="utf-8")
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


def test_security_workflow_runs_for_every_main_and_staging_push():
    workflow = (ROOT / ".github/workflows" / "security.yml").read_text(encoding="utf-8")
    push_block = workflow.split("  push:\n", 1)[1].split("  workflow_dispatch:", 1)[0]

    assert "branches: [main, staging]" in push_block
    assert "paths:" not in push_block
