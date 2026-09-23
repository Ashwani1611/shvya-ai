import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from django.core.cache import cache
from django.http import Http404
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings

from apps.core.fairness import admit, admit_ai_start, admit_provider_start
from apps.core import fairness, observability
from apps.core.observability import RequestObservabilityMiddleware, metrics_snapshot
from apps.core.runtime_status import runtime_metrics
from apps.core.scale_middleware import TenantConcurrencyMiddleware


LOC_MEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "scalability-controls",
    }
}


@override_settings(CACHES=LOC_MEM_CACHE)
class ScalabilityControlTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()

    def test_request_metrics_add_a_server_generated_correlation_id(self):
        request = self.factory.get("/health/live/", HTTP_X_REQUEST_ID="not-a-uuid")
        response = RequestObservabilityMiddleware(lambda _request: HttpResponse("ok"))(
            request
        )

        uuid.UUID(response["X-Request-ID"])
        self.assertNotEqual(response["X-Request-ID"], "not-a-uuid")
        self.assertTrue(metrics_snapshot()["series"])

    def test_request_metric_labels_exclude_customer_identifiers(self):
        request = self.factory.get("/health/live/")
        request.crm_user = SimpleNamespace(
            id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
        )
        RequestObservabilityMiddleware(lambda _request: HttpResponse("ok"))(request)

        request_series = [
            item
            for item in metrics_snapshot()["series"]
            if item["metric"] == "http.requests"
        ]
        self.assertTrue(request_series)
        for item in request_series:
            self.assertNotIn("organization_id", item["labels"])
            self.assertNotIn("user_id", item["labels"])

    @patch("apps.core.observability.MAX_REGISTERED_SERIES", 2)
    def test_metric_series_cap_prevents_unregistered_redis_keys(self):
        observability.increment("bounded.metric", labels={"slot": "one"})
        observability.increment("bounded.metric", labels={"slot": "two"})
        _, _, rejected_series = observability._series(
            "bounded.metric",
            {"slot": "three"},
        )
        observability.increment("bounded.metric", labels={"slot": "three"})

        snapshot = metrics_snapshot()
        self.assertEqual(len(snapshot["series"]), 2)
        self.assertIsNone(
            cache.get(f"{observability.METRIC_PREFIX}:counter:{rejected_series}")
        )

    @override_settings(OBSERVABILITY_TOKEN="monitor-secret")
    @patch("apps.core.runtime_status._hosted_gateway_snapshot", return_value={})
    @patch("apps.core.runtime_status._websocket_snapshot", return_value={})
    @patch("apps.core.runtime_status._celery_snapshot", return_value={})
    @patch("apps.core.runtime_status._redis_snapshot", return_value={})
    @patch("apps.core.runtime_status._database_snapshot", return_value={})
    def test_runtime_metrics_are_hidden_without_the_monitor_token(self, *_snapshots):
        missing = self.factory.get("/health/runtime-metrics/")
        with self.assertRaises(Http404):
            runtime_metrics(missing)

        authorized = self.factory.get(
            "/health/runtime-metrics/",
            HTTP_X_SHVYA_OBSERVABILITY_TOKEN="monitor-secret",
        )
        response = runtime_metrics(authorized)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)["status"], "ok")

    @patch(
        "apps.channels.hosted_gateway_routing.configured_gateways",
        return_value={"east": "http://gateway-east:3000"},
    )
    @patch("apps.core.runtime_status.requests.get")
    def test_runtime_snapshot_includes_bounded_hosted_gateway_health(
        self,
        get,
        _gateways,
    ):
        from apps.core.runtime_status import _hosted_gateway_snapshot

        response = get.return_value
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "ok": True,
            "shard": "east",
            "owner": "east-1",
            "sessions": 7,
            "maxSessions": 25,
            "capacityRemaining": 18,
            "memoryBytes": {
                "rss": 123456,
                "heapUsed": 23456,
                "external": 3456,
            },
            "cpuMicros": {"user": 100, "system": 20},
            "metrics": {
                "reconnects": 2,
                "leaseConflicts": 1,
                "callbacksFailed": 3,
                "historySyncFailures": 4,
                "sessionsCreated": 8,
            },
            "uptimeSeconds": 900,
            "unexpectedSensitiveField": "must-not-leak",
        }

        snapshot = _hosted_gateway_snapshot()

        self.assertTrue(snapshot["available"])
        gateway = snapshot["gateways"]["east"]
        self.assertEqual(gateway["sessions"], 7)
        self.assertEqual(gateway["max_sessions"], 25)
        self.assertEqual(gateway["lease_conflicts"], 1)
        self.assertNotIn("unexpectedSensitiveField", gateway)
        get.assert_called_once_with("http://gateway-east:3000/health", timeout=3)

    def test_shared_fixed_window_admission_rejects_only_after_the_limit(self):
        self.assertEqual(
            admit(scope="test", subject="org-1", limit=2, window_seconds=60)[:1],
            (True,),
        )
        self.assertEqual(
            admit(scope="test", subject="org-1", limit=2, window_seconds=60)[:1],
            (True,),
        )
        allowed, retry_after = admit(
            scope="test", subject="org-1", limit=2, window_seconds=60
        )
        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0)
        self.assertTrue(
            admit(scope="test", subject="org-2", limit=2, window_seconds=60)[0]
        )

    def test_ai_tenant_limit_does_not_consume_other_tenants_global_capacity(self):
        self.assertEqual(
            admit_ai_start(
                organization_id="org-1",
                organization_limit=1,
                global_limit=2,
            )[:1],
            (True,),
        )
        allowed, _, scope = admit_ai_start(
            organization_id="org-1",
            organization_limit=1,
            global_limit=2,
        )
        self.assertFalse(allowed)
        self.assertEqual(scope, "organization")

        # The rejected second start from org-1 must not have consumed the
        # second global slot, so a different tenant can still start.
        allowed, _, scope = admit_ai_start(
            organization_id="org-2",
            organization_limit=1,
            global_limit=2,
        )
        self.assertTrue(allowed)
        self.assertEqual(scope, "")

    def test_provider_account_limit_does_not_consume_global_capacity(self):
        self.assertTrue(
            admit_provider_start(
                provider="whatsapp",
                account_id="account-1",
                account_limit=1,
                global_limit=2,
            )[0]
        )
        allowed, _, scope = admit_provider_start(
            provider="whatsapp",
            account_id="account-1",
            account_limit=1,
            global_limit=2,
        )
        self.assertFalse(allowed)
        self.assertEqual(scope, "account")

        allowed, _, scope = admit_provider_start(
            provider="whatsapp",
            account_id="account-2",
            account_limit=1,
            global_limit=2,
        )
        self.assertTrue(allowed)
        self.assertEqual(scope, "")

    def test_global_rejection_releases_account_reservation(self):
        self.assertTrue(
            admit_provider_start(
                provider="whatsapp",
                account_id="account-a",
                account_limit=1,
                global_limit=1,
            )[0]
        )
        allowed, _, scope = admit_provider_start(
            provider="whatsapp",
            account_id="account-b",
            account_limit=1,
            global_limit=1,
        )
        self.assertFalse(allowed)
        self.assertEqual(scope, "global")

        # Simulate the next provider window while leaving account-b's bucket
        # untouched. It must be reusable because the global rejection rolled
        # its reservation back.
        cache.delete(fairness._bucket_key("whatsapp_global", "platform", 60))
        allowed, _, scope = admit_provider_start(
            provider="whatsapp",
            account_id="account-b",
            account_limit=1,
            global_limit=1,
        )
        self.assertTrue(allowed)
        self.assertEqual(scope, "")

    @override_settings(TENANT_HTTP_CONCURRENCY_LIMIT=1)
    def test_tenant_concurrency_rejects_a_busy_tenant_and_preserves_others(self):
        org_id = uuid.uuid4()
        request = self.factory.get("/expensive/")
        request.crm_user = SimpleNamespace(organization_id=org_id)
        digest_key = __import__("hashlib").sha256(str(org_id).encode()).hexdigest()[:24]
        cache.set(f"shvya:http-concurrency:{digest_key}", 1, timeout=120)

        response = TenantConcurrencyMiddleware(lambda _request: HttpResponse("ok"))(
            request
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "1")


class ProductionPoolingSettingsTests(SimpleTestCase):
    def _load_database_settings(self, **overrides):
        env = os.environ.copy()
        env.update(
            {
                "SECRET_KEY": "test-secret-key",
                "JWT_SECRET": "test-jwt-secret",
                "CREDENTIAL_ENCRYPTION_KEY": "test-credential-key",
                "REDIS_URL": "redis://localhost:6379/0",
                **overrides,
            }
        )
        root = Path(__file__).resolve().parents[3]
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json; from config.settings import prod; "
                    "print(json.dumps(prod.DATABASES['default']))"
                ),
            ],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_transaction_pooling_disables_persistent_and_server_side_connections(self):
        database = self._load_database_settings(DB_USE_PGBOUNCER="True")
        self.assertEqual(database["CONN_MAX_AGE"], 0)
        self.assertTrue(database["DISABLE_SERVER_SIDE_CURSORS"])

    def test_direct_database_can_use_a_bounded_persistent_connection(self):
        database = self._load_database_settings(
            DB_USE_PGBOUNCER="False", DB_CONN_MAX_AGE="45"
        )
        self.assertEqual(database["CONN_MAX_AGE"], 45)
        self.assertNotIn("DISABLE_SERVER_SIDE_CURSORS", database)
