from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _service_block(filename, service, next_service):
    content = (ROOT / filename).read_text(encoding="utf-8")
    start = content.index(f"\n  {service}:\n")
    end = content.index(f"\n  {next_service}:\n", start)
    return content[start:end]


@pytest.mark.parametrize(
    ("filename", "service", "next_service"),
    [
        ("docker-compose.yml", "web", "ws"),
        ("docker-compose.yml", "ws", "worker"),
        ("docker-compose.staging.yml", "web", "ws"),
        ("docker-compose.staging.yml", "ws", "worker"),
    ],
)
def test_web_and_asgi_services_have_no_fixed_replica_identity(
    filename,
    service,
    next_service,
):
    block = _service_block(filename, service, next_service)
    assert "container_name:" not in block
    assert "\n    ports:" not in block


@pytest.mark.parametrize(
    ("service", "next_service", "replica_var"),
    [
        ("web", "ws", "WEB_REPLICAS"),
        ("ws", "worker", "ASGI_REPLICAS"),
        ("worker", "ai_realtime_worker", "CELERY_GENERAL_REPLICAS"),
        ("ai_realtime_worker", "hosted_ai_worker", "CELERY_AI_REALTIME_REPLICAS"),
        ("hosted_ai_worker", "campaign_worker", "CELERY_HOSTED_AI_REPLICAS"),
        ("campaign_worker", "ingestion_worker", "CELERY_CAMPAIGN_REPLICAS"),
        ("ingestion_worker", "automation_worker", "CELERY_INGESTION_REPLICAS"),
        ("automation_worker", "beat", "CELERY_AUTOMATION_REPLICAS"),
    ],
)
@pytest.mark.parametrize(
    "filename",
    ["docker-compose.yml", "docker-compose.staging.yml"],
)
def test_application_replica_counts_are_declarative(
    filename,
    service,
    next_service,
    replica_var,
):
    block = _service_block(filename, service, next_service)
    assert "deploy:" in block
    assert "replicas: ${" + replica_var + ":-1}" in block
    assert "scale: ${" + replica_var + ":-1}" in block

@pytest.mark.parametrize(
    "filename",
    [
        "nginx/conf.d/dashboard.conf",
        "nginx/staging/default.conf",
    ],
)
def test_nginx_resolves_web_and_asgi_membership_dynamically(filename):
    content = (ROOT / filename).read_text(encoding="utf-8")
    assert "resolver 127.0.0.11" in content
    assert "set $django_web http://web:8000;" in content
    assert "set $django_ws http://ws:8001;" in content
    assert "proxy_pass http://web:8000;" not in content
    assert "proxy_pass http://ws:8001;" not in content
    assert "proxy_pass $django_web;" in content
    assert "proxy_pass $django_ws;" in content


def test_legacy_production_hosts_proxy_api_without_redirecting_posts():
    content = (ROOT / "nginx/conf.d/dashboard.conf").read_text(encoding="utf-8")
    marker = "server_name www.shvya-ai.com dashboard.shvya-ai.com;"
    start = content.index(marker)
    end = content.index("\nserver {", start)
    legacy = content[start:end]
    assert "location ^~ /api/" in legacy
    assert "proxy_pass $django_web;" in legacy
    assert "proxy_set_header Host shvya-ai.com;" in legacy
    assert "return 301 https://shvya-ai.com$request_uri;" in legacy
