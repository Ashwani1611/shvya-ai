"""Metadata-only HTTP activity capture after SHVYA's area authentication."""

from datetime import timedelta
import time

from django.conf import settings
from django.utils import timezone

from apps.core.observability import REQUEST_ID

from .services import activity_context, annotate_activity, finish_activity, start_activity


def request_integration(path):
    """Classify application boundaries without persisting private URL values."""
    path = path.lower()
    for fragment, integration in (
        ("indiamart", "indiamart"), ("justdial", "justdial"),
        ("meta-lead", "meta_leads"), ("instagram", "instagram"),
        ("hosted", "whatsapp_hosted"), ("whatsapp", "whatsapp_api"),
        ("99acres", "99acres"), ("mcp", "shvya_mcp"),
    ):
        if fragment in path:
            return integration
    return "shvya_api"


def should_capture(request):
    path = request.path_info
    if path.startswith("/superadmin/api/"):
        return False  # Browsing diagnostics must not generate its own traffic.
    if request.method in {"GET", "HEAD", "OPTIONS"} and path.startswith(("/dashboard/", "/superadmin/")):
        # Dashboard polls and page loads already have bounded request metrics.
        # Keep the durable history focused on APIs and operational mutations.
        return False
    return path.startswith((
        "/api/", "/webhooks/", "/dashboard/", "/superadmin/",
        "/mcp/", "/diagnostics/", "/oauth/", "/vault/api/",
    ))


def authenticated_organization_id(request):
    if request.path_info.startswith("/webhooks/"):
        return None  # A browser cookie cannot authenticate a provider callback.
    user = getattr(request, "user", None)
    if not request.path_info.startswith(("/api/", "/webhooks/")):
        user = getattr(request, "crm_user", None) or user
    if user is not None and getattr(user, "is_authenticated", False):
        return getattr(user, "organization_id", None) or getattr(getattr(user, "organization", None), "pk", None)
    # Do not infer tenant from URL, payload or user-supplied headers.
    return None


class APIActivityMiddleware:
    """Record HTTP transport independently of downstream business outcomes."""

    def __init__(self, get_response):
        self.get_response = get_response

    def process_view(self, request, view_func, view_args, view_kwargs):
        activity = getattr(request, "shvya_api_activity", None)
        match = getattr(request, "resolver_match", None)
        if activity and match:
            # resolver.route contains placeholders, never the actual secret
            # webhook token or identifiers substituted into a request path.
            annotate_activity(
                activity,
                operation=match.view_name or "http.request",
                endpoint=str(getattr(match, "route", "") or match.view_name),
            )

    def __call__(self, request):
        if not getattr(settings, "API_ACTIVITY_ENABLED", True) or not should_capture(request):
            return self.get_response(request)
        initial_user = getattr(request, "user", None)
        is_webhook = "webhook" in request.path_info.lower()
        activity = start_activity(
            integration=request_integration(request.path_info),
            operation="webhook.receive" if is_webhook else "http.request",
            kind="webhook" if is_webhook else "http_request",
            direction="inbound",
            # DRF authenticates API keys later; a browser cookie must not assign
            # a tenant to that API request or a provider webhook beforehand.
            organization_id=(None if request.path_info.startswith(("/api/", "/webhooks/"))
                             else authenticated_organization_id(request)),
            request_id=REQUEST_ID.get(),
            method=request.method,
            endpoint="unresolved",
            expected_by=timezone.now() + timedelta(seconds=max(
                1, int(getattr(settings, "API_ACTIVITY_REQUEST_DEADLINE_SECONDS", 60)),
            )),
            next_step="HTTP response",
        )
        request.shvya_api_activity = activity
        started = time.perf_counter()
        with activity_context(activity):
            try:
                response = self.get_response(request)
            except Exception as exc:
                finish_activity(
                    activity, status="failed", http_status=500,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    error_category="application", error_code=type(exc).__name__,
                    summary="Request raised an application exception.",
                )
                raise
            else:
                # DRF may only establish the identity while executing the view.
                organization_id = authenticated_organization_id(request)
                if request.path_info.startswith("/api/") and getattr(request, "user", None) is initial_user:
                    # Public API callbacks may leave the ambient session user
                    # untouched. Only a newly authenticated principal may own
                    # this boundary; domain observers resolve their own tenant.
                    organization_id = None
                if organization_id and organization_id != getattr(activity, "organization_id", None):
                    annotate_activity(activity, organization_id=organization_id)
                code = response.status_code
                if code in (401, 403, 429):
                    status = "blocked"
                elif code >= 400:
                    status = "failed"
                elif is_webhook or (200 <= code < 300 and request.method not in ("GET", "HEAD", "OPTIONS")):
                    status = "accepted"
                else:
                    status = "completed"
                if status == "accepted":
                    # This deadline described the HTTP response, which arrived.
                    # Business confirmation belongs to the downstream activity.
                    annotate_activity(activity, expected_by=None, next_step="")
                finish_activity(
                    activity, status=status, http_status=code,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    error_category=("http" if code >= 400 else ""),
                    error_code=(str(code) if code >= 400 else ""),
                    summary=(
                        "HTTP request acknowledged; business outcomes are recorded separately."
                        if status == "accepted" else f"HTTP request finished with status {code}."
                    ),
                )
                if activity:
                    response["X-SHVYA-Activity-ID"] = str(activity.id)
                return response
