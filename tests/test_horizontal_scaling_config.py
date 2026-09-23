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
