"""Clear task-local AI observations at the outer shared execution boundaries."""
from contextlib import contextmanager
from functools import wraps
from importlib import import_module


@contextmanager
def isolated_turn():
    variables = {
        "intent_runtime": ("_CURRENT",),
        "conversation_policy_runtime": ("_TURN", "_POLICY"),
        "phase5_6_runtime": ("_ACTIVE_EVIDENCE", "_ACTIVE_MEMORY"),
    }
    tokens = []
    try:
        for module_name, names in variables.items():
            module = import_module("apps.ai_engagement.services." + module_name)
            for name in names:
                variable = getattr(module, name)
                tokens.append((variable, variable.set(None)))
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def isolated_ai_turn(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with isolated_turn():
            return function(*args, **kwargs)
    return wrapped
