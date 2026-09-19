"""Prevent old application processes from running across schema removals."""

from pathlib import Path
import re
import shlex
from textwrap import dedent

import pytest


ROOT = Path(__file__).resolve().parents[1]
APPLICATION_SERVICES = {
    "web", "ws", "worker", "ai_realtime_worker", "hosted_ai_worker", "beat",
}
WORKFLOWS = [
    ("deploy.yml", "docker compose", "main"),
    ("deploy-staging.yml", "$COMPOSE", "staging"),
]


def _script(filename):
    workflow = (ROOT / ".github/workflows" / filename).read_text(encoding="utf-8")
    return dedent(workflow.split("          script: |\n", 1)[1])


@pytest.mark.parametrize(("filename", "compose", "branch"), WORKFLOWS)
def test_backup_and_application_drain_precede_schema_change(filename, compose, branch):
    script = _script(filename)
    readiness = script.index(f"{compose} up -d --wait db redis")
    backup = script.index("pg_dump")
    checked_backup = script.index('test -s "$BACKUP_FILE"')
    compressed_backup = script.index('gzip "$BACKUP_FILE"')
    producers = script.index(f"{compose} stop --timeout 60 beat web ws")
    workers = script.index(f"{compose} stop --timeout 300 worker ai_realtime_worker hosted_ai_worker")
    stopped_guard = script.index(f"{compose} ps --status running -q web ws worker ai_realtime_worker hosted_ai_worker beat")
    migrate = script.index(f"{compose} run --rm --no-deps web python manage.py migrate --noinput")
    collectstatic = script.index(f"{compose} run --rm --no-deps web python manage.py collectstatic --noinput")
    restart = script.index(f"{compose} up -d --no-deps web ws worker ai_realtime_worker hosted_ai_worker beat")
    assert readiness < backup < checked_backup < compressed_backup < producers < workers < stopped_guard < migrate < collectstatic < restart
    assert "Refusing migrations while an old" in script[stopped_guard:migrate]
    assert "exit 1" in script[stopped_guard:migrate]


@pytest.mark.parametrize(("filename", "compose", "branch"), WORKFLOWS)
def test_drain_stops_only_application_services_with_bounded_timeouts(filename, compose, branch):
    script = _script(filename)
    stopped = set()
    for line in script.splitlines():
        if not line.startswith(f"{compose} stop "):
            continue
        arguments = shlex.split(line[len(compose):])
        assert arguments[:2] == ["stop", "--timeout"]
        assert 0 < int(arguments[2]) <= 300
        stopped.update(arguments[3:])
    assert stopped == APPLICATION_SERVICES
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
    assert "${BACKUP_FILE}.gz" in failure_hook
    assert "return \"$deploy_status\"" in failure_hook
    assert f"{compose} up" not in failure_hook
    maintenance = script.index("APPLICATION_MAINTENANCE=1")
    restart = script.index(f"{compose} up -d --no-deps web ws worker ai_realtime_worker hosted_ai_worker beat")
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
    assert script.index('[ "$ACTUAL_SHA" != "$DEPLOY_SHA" ]') < script.index("pg_dump")
    assert f"git reset --hard origin/{branch}" not in script
