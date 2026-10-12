"""Metadata-only HTTP observations; preserves the caller's transport contract.

Never inspect request headers, query values, bodies, files or successful response
payloads. A provider acknowledgement is not proof of message delivery.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

import requests

from .services import finish_activity, get_context, start_activity

_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,100}\Z")


def _token(value):
    value = str(value) if type(value) in (str, int) else ""
    return value if _SAFE_TOKEN.fullmatch(value) else ""


def response_error_metadata(response):
    """Only bounded structured error codes; never provider free-text/payloads."""
    status = getattr(response, "status_code", None)
    if type(status) is not int or not 400 <= status <= 599:
        return {}
    try:
        # Do not consume large/streamed error bodies just for observability.
        body = getattr(response, "content", b"")
        if not isinstance(body, bytes) or len(body) > 16384:
            return {}
        payload = response.json()
    except Exception:
        return {}
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return {}
    return {key: _token(error.get(source)) for key, source in (
        ("provider_error_code", "code"), ("provider_error_subcode", "error_subcode"),
        ("provider_error_type", "type"), ("provider_trace_id", "fbtrace_id"),
    ) if _token(error.get(source))}


def observed_request(method, url, *, integration, operation, request_callable=None,
                     organization_id=None, connection_id="", reference="", **kwargs):
    """Call a requests verb or requests.request unchanged while recording metadata.

    A passed request_callable is a verb callable (e.g. requests.post) and takes
    URL as its first argument. Omit it for requests.request(method, URL, ...).
    Failure to record telemetry never substitutes for a provider response.
    """
    context = get_context()
    method = str(method).upper()
    # Host only: webhook paths, media URLs and query strings may carry secrets.
    try:
        host = urlsplit(str(url)).hostname or ""
    except ValueError:
        host = ""
    fields = dict(integration=integration, operation=operation, kind="api", direction="outbound",
                  method=method, endpoint=host, connection_id=connection_id or context.get("connection_id", ""),
                  provider_reference=reference, summary="Provider request started.")
    if organization_id:
        fields["organization_id"] = organization_id
    activity = start_activity(**fields)
    started = time.monotonic()
    try:
        response = (request_callable(url, **kwargs) if request_callable is not None
                    else requests.request(method, url, **kwargs))
    except Exception as exc:
        uncertain = method not in {"GET", "HEAD", "OPTIONS"} and isinstance(exc, requests.RequestException)
        finish_activity(activity, status="unknown" if uncertain else "failed",
                        duration_ms=round((time.monotonic() - started) * 1000),
                        error_category="network", error_code=type(exc).__name__,
                        summary=("Provider outcome is unconfirmed; inspect before retrying." if uncertain
                                 else "Provider request failed before a response was available."))
        raise
    status = getattr(response, "status_code", None)
    status = status if type(status) is int and 100 <= status <= 599 else None
    details = {} if kwargs.get("stream") else response_error_metadata(response)
    ok = status is not None and 200 <= status < 300
    finish_activity(activity, status=("accepted" if ok and method not in {"GET", "HEAD", "OPTIONS"}
                                     else "completed" if ok else "failed"),
                    duration_ms=round((time.monotonic() - started) * 1000), http_status=status,
                    error_code=details.get("provider_error_code", ""),
                    error_category="provider" if not ok else "",
                    summary=("Provider acknowledged the HTTP request; delivery is tracked separately." if ok
                             else "Provider returned an unsuccessful HTTP response."), metadata=details)
    return response
