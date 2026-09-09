"""Keep existing service tests independent of the new external verifier call.

The grounding gate has dedicated tests that override this fixture's verdict.
"""
from unittest.mock import patch
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def grounding_provider():
    with patch("apps.ai_engagement.graph.evidence.OpenAIProvider") as provider:
        provider.return_value.generate_text.return_value = SimpleNamespace(
            text='{"approved": true, "reason": "supported"}', model="test-verifier",
        )
        yield provider

