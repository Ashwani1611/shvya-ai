def test_liveness_endpoint_is_public(client):
    response = client.get("/health/live/")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "environment" in payload


def test_readiness_endpoint_checks_database_and_redis(client):
    response = client.get("/health/ready/")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["checks"] == {
        "database": True,
        "redis": True,
    }
