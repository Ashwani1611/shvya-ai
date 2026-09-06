from contextlib import contextmanager
from contextvars import ContextVar


_TRIGGER_SIGNALS_SUPPRESSED = ContextVar(
    "shvya_trigger_signals_suppressed",
    default=False,
)


def trigger_signals_suppressed():
    return bool(_TRIGGER_SIGNALS_SUPPRESSED.get())


@contextmanager
def suppress_trigger_signals():
    """Prevent Smart Trigger actions from recursively emitting lead events."""
    token = _TRIGGER_SIGNALS_SUPPRESSED.set(True)
    try:
        yield
    finally:
        _TRIGGER_SIGNALS_SUPPRESSED.reset(token)
