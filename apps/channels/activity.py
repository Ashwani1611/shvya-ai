"""Channel-owned activity observations without message content or credentials."""
from functools import wraps
import time

import requests

from apps.core.api_activity.services import activity_context, finish_activity, start_activity


def observe_message_send(integration):
    """Observe canonical send boundary while preserving transport behavior."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            message = kwargs.get("message") or (args[0] if args else None)
            if message is None:
                return function(*args, **kwargs)
            activity = start_activity(
                integration=integration, operation="message.send", kind="message", direction="outbound",
                organization_id=getattr(message, "organization_id", None),
                connection_id=str(getattr(message, "account_id", getattr(getattr(message, "account", None), "pk", "")) or ""),
                entity_type="message", entity_id=str(getattr(message, "pk", "") or ""),
                provider_reference=getattr(message, "external_id", "") or "",
                metadata={"message_id": str(getattr(message, "pk", "") or ""), "lead_id": str(getattr(message, "lead_id", "") or "")},
                summary="Message send attempt started.",
            )
            started = time.monotonic()
            with activity_context(activity):
                try:
                    result = function(*args, **kwargs)
                except Exception as exc:
                    cause = exc
                    uncertain = False
                    for _ in range(5):
                        if isinstance(cause, requests.RequestException):
                            uncertain = True
                            break
                        cause = getattr(cause, "__cause__", None)
                        if cause is None:
                            break
                    deferred = type(exc).__name__ in {"HostedAutomationPaused", "AIMessageDeferred"}
                    finish_activity(activity, status="skipped" if deferred else "unknown" if uncertain else "failed",
                                    duration_ms=round((time.monotonic() - started) * 1000),
                                    error_code=type(exc).__name__, error_category="policy" if deferred else "network" if uncertain else "execution",
                                    summary=("This send attempt was deferred by runtime policy." if deferred
                                             else "Message send outcome is unconfirmed; inspect before retrying." if uncertain
                                             else "Message send did not complete; inspect linked request and state evidence."))
                    raise
                observed_message = result if hasattr(result, "status") and hasattr(result, "external_id") else message
                state = getattr(observed_message, "status", "")
                status = {"sent": "accepted", "delivered": "completed", "read": "completed", "failed": "failed", "queued": "queued"}.get(state, "unknown")
                finish_activity(activity, status=status, duration_ms=round((time.monotonic() - started) * 1000),
                                provider_reference=observed_message.external_id or "",
                                summary="Message send returned; provider delivery receipts remain separate evidence.",
                                metadata={"message_status": state})
                return result
        return wrapped
    return decorate
