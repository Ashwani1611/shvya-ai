"""HTTP and worker observations must preserve application behavior and privacy."""

from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch
import uuid

from django.http import HttpResponse
from django.db import close_old_connections
from django.test import RequestFactory, SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.core.api_activity import celery as activity_tasks
from apps.core.api_activity.middleware import APIActivityMiddleware
from apps.core.api_activity.services import activity_context, get_context
from apps.core.models import APIActivity
from apps.core.observability import REQUEST_ID, TASK_ID


def activity_fixture(**fields):
    return APIActivity(id=uuid.uuid4(), correlation_id=uuid.uuid4(), **fields)


@override_settings(API_ACTIVITY_ENABLED=True)
class APIActivityMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.activity = activity_fixture()
        self.start = patch(
            "apps.core.api_activity.middleware.start_activity", return_value=self.activity,
        ).start()
        self.finish = patch("apps.core.api_activity.middleware.finish_activity").start()
        self.annotate = patch("apps.core.api_activity.middleware.annotate_activity").start()
        self.addCleanup(patch.stopall)

    def test_diagnostics_and_health_do_not_record_their_own_requests(self):
        for path in ("/superadmin/api/", "/superadmin/api/activities/", "/health/live/"):
            with self.subTest(path=path):
                response = HttpResponse("ok")
                result = APIActivityMiddleware(lambda request: response)(self.factory.get(path))
                self.assertIs(result, response)
        self.start.assert_not_called()
        self.finish.assert_not_called()

    def test_dashboard_and_superadmin_read_polls_do_not_create_durable_rows(self):
        for path in ("/dashboard/whatsapp/poll/", "/superadmin/organizations/"):
            for method in ("get", "head", "options"):
                with self.subTest(path=path, method=method):
                    request = getattr(self.factory, method)(path)
                    APIActivityMiddleware(lambda request: HttpResponse("ok"))(request)
        self.start.assert_not_called()
        self.finish.assert_not_called()

    def test_route_template_is_recorded_without_query_body_or_webhook_secret(self):
        request = self.factory.post(
            "/webhooks/justdial/private-webhook-token/?access_token=private-query-token",
            data={"body": "private customer message"},
            HTTP_AUTHORIZATION="Bearer private-header-token",
        )
        request.resolver_match = SimpleNamespace(
            route="webhooks/justdial/<uuid:token>/", view_name="justdial-webhook",
        )

        def view(request):
            middleware.process_view(request, None, (), {})
            self.assertEqual(get_context()["activity_id"], str(self.activity.id))
            return HttpResponse("acknowledged")

        middleware = APIActivityMiddleware(view)
        response = middleware(request)
        self.assertEqual(self.start.call_args.kwargs["endpoint"], "unresolved")
        route_observations = [call.kwargs for call in self.annotate.call_args_list if "endpoint" in call.kwargs]
        self.assertEqual(route_observations, [{
            "operation": "justdial-webhook", "endpoint": request.resolver_match.route,
        }])
        self.assertTrue(any(
            call.kwargs == {"expected_by": None, "next_step": ""}
            for call in self.annotate.call_args_list
        ))
        self.assertEqual(self.finish.call_args.kwargs["status"], "accepted")
        self.assertEqual(response["X-SHVYA-Activity-ID"], str(self.activity.id))
        captured = repr((self.start.call_args, self.finish.call_args, self.annotate.call_args_list))
        for private in ("private-webhook-token", "private-query-token", "private-header-token", "private customer message"):
            self.assertNotIn(private, captured)
        self.assertEqual(get_context(), {})

    def test_transport_status_does_not_claim_business_completion_for_writes(self):
        for method, code, expected in (
            ("get", 200, "completed"), ("post", 200, "accepted"),
            ("post", 202, "accepted"), ("post", 403, "blocked"),
            ("post", 429, "blocked"), ("post", 500, "failed"),
        ):
            with self.subTest(method=method, code=code):
                request = getattr(self.factory, method)("/api/v1/leads/")
                APIActivityMiddleware(lambda request: HttpResponse(status=code))(request)
                self.assertEqual(self.finish.call_args.kwargs["status"], expected)
                self.assertEqual(self.finish.call_args.kwargs["http_status"], code)

    def test_exception_preserves_original_error_and_restores_outer_context(self):
        outer = activity_fixture()
        error = RuntimeError("private request body and provider token")

        def view(request):
            raise error

        with activity_context(outer):
            with self.assertRaises(RuntimeError) as caught:
                APIActivityMiddleware(view)(self.factory.get("/api/v1/leads/"))
            self.assertIs(caught.exception, error)
            self.assertEqual(get_context()["activity_id"], str(outer.id))
        self.assertEqual(get_context(), {})
        self.assertEqual(self.finish.call_args.kwargs["status"], "failed")
        self.assertEqual(self.finish.call_args.kwargs["error_code"], "RuntimeError")
        self.assertNotIn(str(error), repr(self.finish.call_args))

    def test_tenant_is_resolved_from_authenticated_identity_after_view(self):
        tenant_id = uuid.uuid4()
        request = self.factory.get(
            "/api/v1/leads/", HTTP_X_ORGANIZATION_ID=str(uuid.uuid4()),
        )
        request.user = SimpleNamespace(is_authenticated=False, organization_id=uuid.uuid4())

        def view(request):
            request.user = SimpleNamespace(is_authenticated=True, organization_id=tenant_id)
            return HttpResponse("ok")

        APIActivityMiddleware(view)(request)
        self.assertIsNone(self.start.call_args.kwargs["organization_id"])
        self.assertEqual(self.annotate.call_args.kwargs["organization_id"], tenant_id)

    def test_api_key_principal_overrides_ambient_dashboard_tenant_after_authentication(self):
        from apps.crm.authentication import APIKeyPrincipal

        tenant_id = uuid.uuid4()
        request = self.factory.get("/api/v1/leads/")
        request.crm_user = SimpleNamespace(is_authenticated=True, organization_id=uuid.uuid4())
        request.user = request.crm_user
        api_key = SimpleNamespace(organization=SimpleNamespace(pk=tenant_id))

        def view(request):
            request.user = APIKeyPrincipal(api_key)
            return HttpResponse("ok")

        APIActivityMiddleware(view)(request)
        self.assertIsNone(self.start.call_args.kwargs["organization_id"])
        self.assertEqual(self.annotate.call_args.kwargs["organization_id"], tenant_id)

    def test_provider_webhook_never_inherits_ambient_browser_organization(self):
        request = self.factory.post("/webhooks/whatsapp/")
        request.user = SimpleNamespace(is_authenticated=True, organization_id=uuid.uuid4())
        request.crm_user = request.user
        APIActivityMiddleware(lambda request: HttpResponse("acknowledged"))(request)
        self.assertIsNone(self.start.call_args.kwargs["organization_id"])
        self.assertFalse(any("organization_id" in call.kwargs for call in self.annotate.call_args_list))

    def test_public_api_with_unchanged_browser_identity_remains_unattributed(self):
        request = self.factory.get("/api/public/status/")
        request.user = SimpleNamespace(is_authenticated=True, organization_id=uuid.uuid4())
        request.crm_user = request.user
        APIActivityMiddleware(lambda request: HttpResponse("ok"))(request)
        self.assertIsNone(self.start.call_args.kwargs["organization_id"])
        self.assertFalse(any("organization_id" in call.kwargs for call in self.annotate.call_args_list))

    def test_capture_storage_failure_preserves_response_and_outer_context(self):
        self.start.return_value = None
        response = HttpResponse("business operation completed")
        outer = activity_fixture()
        with activity_context(outer):
            result = APIActivityMiddleware(lambda request: response)(self.factory.get("/api/v1/leads/"))
            self.assertEqual(get_context()["activity_id"], str(outer.id))
        self.assertIs(result, response)
        self.assertNotIn("X-SHVYA-Activity-ID", result)
        self.assertIsNone(self.finish.call_args.args[0])


@override_settings(API_ACTIVITY_ENABLED=True)
class APIActivityWorkerTests(SimpleTestCase):
    def setUp(self):
        self.activity = activity_fixture()
        self.start = patch("apps.core.api_activity.celery.start_activity", return_value=self.activity).start()
        self.finish = patch("apps.core.api_activity.celery.finish_activity").start()
        self.annotate = patch("apps.core.api_activity.celery.annotate_activity").start()
        self.event = patch("apps.core.api_activity.celery.record_event").start()
        self.claim = patch("apps.core.api_activity.celery._claim_published_activity", return_value=(None, False)).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        while activity_tasks._EXECUTIONS.get():
            activity_tasks.finish_execution(activity_tasks._EXECUTIONS.get()[-1][0], "FAILURE")

    @override_settings(API_ACTIVITY_QUEUE_DEADLINE_SECONDS=300)
    def test_scheduled_eta_sets_deadline_after_due_time_without_copying_arguments(self):
        now = timezone.now()
        eta = now + timedelta(hours=2)
        headers = {
            "id": "scheduled-task", "eta": eta.isoformat(), "retries": 1,
            "argsrepr": "private customer text", "kwargsrepr": "private token",
        }
        with patch("apps.core.api_activity.celery.timezone.now", return_value=now):
            activity_tasks.publish_activity("apps.channels.tasks.send_whatsapp_message_task", headers, "ai_realtime")
        recorded = self.start.call_args.kwargs
        self.assertEqual(recorded["expected_by"], eta + timedelta(seconds=300))
        self.assertEqual(recorded["metadata"]["attempt"], 2)
        self.assertEqual(recorded["status"], "queued")
        self.assertNotIn("private", repr(recorded))
        self.assertEqual(headers["shvya_activity_id"], str(self.activity.id))

    def test_nested_execution_restores_request_task_and_activity_context(self):
        outer = activity_fixture()
        with activity_context(outer):
            request_token = REQUEST_ID.set("outer-request")
            task_token = TASK_ID.set("outer-task")
            try:
                activity_tasks.start_execution("inner-task", "test.task", {"shvya_request_id": "inner-request"})
                self.assertEqual(REQUEST_ID.get(), "inner-request")
                self.assertEqual(TASK_ID.get(), "inner-task")
                self.assertEqual(get_context()["activity_id"], str(self.activity.id))
                activity_tasks.finish_execution("inner-task", "SUCCESS", duration_ms=12)
                self.assertEqual(REQUEST_ID.get(), "outer-request")
                self.assertEqual(TASK_ID.get(), "outer-task")
                self.assertEqual(get_context()["activity_id"], str(outer.id))
                self.assertIsNone(activity_tasks.current_activity())
            finally:
                REQUEST_ID.reset(request_token)
                TASK_ID.reset(task_token)
        self.assertEqual(get_context(), {})

    def test_failure_and_retry_store_exception_type_without_message_or_success_overwrite(self):
        for state in ("FAILURE", "RETRY"):
            with self.subTest(state=state):
                self.finish.reset_mock()
                activity_tasks.start_execution("task-1", "test.task", {})
                error = RuntimeError("private credentials and customer message")
                if state == "FAILURE":
                    activity_tasks.fail_execution("task-1", error)
                else:
                    activity_tasks.retry_execution("task-1", error)
                activity_tasks.finish_execution("task-1", state, duration_ms=20)
                self.finish.assert_called_once()
                self.assertEqual(self.finish.call_args.kwargs["status"], "failed")
                self.assertEqual(self.finish.call_args.kwargs["error_code"], "RuntimeError")
                self.assertNotIn(str(error), repr(self.finish.call_args))
                self.assertEqual(get_context(), {})

    def test_unknown_or_revoked_state_is_not_completed(self):
        for state, expected in ((None, "unknown"), ("REVOKED", "blocked")):
            with self.subTest(state=state):
                activity_tasks.start_execution("task-1", "test.task", {})
                activity_tasks.finish_execution("task-1", state)
                self.assertEqual(self.finish.call_args.kwargs["status"], expected)

    def test_redelivery_creates_new_attempt_linked_to_existing_activity(self):
        prior = activity_fixture(status="completed", organization_id=uuid.uuid4())
        self.claim.return_value = prior, False
        observed = []

        def create_attempt(**fields):
            observed.append(get_context())
            return self.activity

        self.start.side_effect = create_attempt
        activity_tasks.start_execution("redelivered-task", "test.task", {})
        activity_tasks.finish_execution("redelivered-task", "SUCCESS")
        self.assertEqual(observed[0]["activity_id"], str(prior.id))
        self.assertEqual(observed[0]["correlation_id"], str(prior.correlation_id))
        self.assertEqual(observed[0]["organization_id"], str(prior.organization_id))
        self.assertEqual(self.finish.call_args.args[0], self.activity)

    def test_missing_storage_does_not_block_worker_or_leak_context(self):
        self.start.return_value = None
        activity_tasks.start_execution("task-no-storage", "test.task", {})
        self.assertIsNone(activity_tasks.current_activity())
        activity_tasks.finish_execution("task-no-storage", "SUCCESS")
        self.assertEqual(get_context(), {})
        self.assertEqual(activity_tasks._EXECUTIONS.get(), ())

    def test_celery_signals_ignore_task_arguments_results_and_error_messages(self):
        from config import celery_observability as signals

        task = SimpleNamespace(name="test.task", request=SimpleNamespace(headers={}))
        with (
            patch.object(signals, "_broker"),
            patch.object(signals, "increment"),
            patch.object(signals, "observe_latency"),
            patch.object(signals, "emit_event") as emit,
        ):
            signals.task_started(
                sender=task, task_id="task-1", task=task,
                args=["private task argument"], kwargs={"token": "private credential"},
            )
            signals.task_failed(
                sender=task, task_id="task-1",
                exception=RuntimeError("private error payload"),
                args=["private task argument"], kwargs={"token": "private credential"},
                traceback="private traceback",
            )
            signals.task_finished(
                sender=task, task_id="task-1", task=task, state="FAILURE",
                retval={"body": "private task result"},
            )
        captured = repr((self.start.call_args, self.finish.call_args, emit.call_args))
        self.assertNotIn("private", captured)
        self.assertEqual(self.finish.call_args.kwargs["status"], "failed")
        self.assertEqual(get_context(), {})


class APIActivityWorkerStorageTests(SimpleTestCase):
    def test_task_lookup_failure_returns_none_without_secret_exception_content(self):
        with (
            patch("apps.core.api_activity.celery.transaction.atomic", return_value=nullcontext()),
            patch("apps.core.api_activity.celery.apps.get_model", side_effect=RuntimeError("private DB connection secret")),
            self.assertLogs("shvya.operations", level="WARNING") as logs,
        ):
            result = activity_tasks._published_activity({"shvya_activity_id": str(uuid.uuid4())})
        self.assertIsNone(result)
        self.assertIn("task_lookup_failed", " ".join(logs.output))
        self.assertNotIn("private", " ".join(logs.output))

    def test_invalid_activity_id_does_not_query_database(self):
        with patch("apps.core.api_activity.celery.apps.get_model") as lookup:
            self.assertIsNone(activity_tasks._published_activity({"shvya_activity_id": "private-header"}))
        lookup.assert_not_called()

    @override_settings(API_ACTIVITY_ENABLED=False)
    def test_disabled_capture_does_not_lookup_or_claim_task_rows(self):
        headers = {"shvya_activity_id": str(uuid.uuid4())}
        with patch("apps.core.api_activity.celery.apps.get_model") as lookup:
            self.assertIsNone(activity_tasks._published_activity(headers))
            self.assertEqual(activity_tasks._claim_published_activity(headers), (None, False))
        lookup.assert_not_called()


@override_settings(API_ACTIVITY_ENABLED=True)
class APIActivityWorkerConcurrentClaimTests(TransactionTestCase):
    def test_concurrent_deliveries_claim_queued_attempt_only_once(self):
        activity = APIActivity.objects.create(operation="test.concurrent", status="queued", kind="task")
        barrier = Barrier(2)

        def claim():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                row, claimed = activity_tasks._claim_published_activity({"shvya_activity_id": str(activity.pk)})
                return row.pk if row is not None else None, claimed
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim) for _ in range(2)]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual(sum(claimed for _, claimed in results), 1)
        self.assertEqual({identifier for identifier, _ in results}, {activity.pk})
        activity.refresh_from_db()
        self.assertEqual(activity.status, "processing")
