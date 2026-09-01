from __future__ import annotations

import pytest

from apps.ai_engagement.services.base_instructions import (
    BaseInstructionsError,
    SHVYABaseInstructions,
)


class TestSHVYABaseInstructions:
    def test_get_returns_non_empty_instructions(self):
        instructions = SHVYABaseInstructions.get()

        assert instructions
        assert instructions.strip()

    def test_instructions_define_shvya_role(self):
        instructions = SHVYABaseInstructions.get()

        assert "You are SHVYA AI" in instructions

    def test_instructions_contain_general_behavior_rules(self):
        instructions = SHVYABaseInstructions.get()

        assert "Do not invent facts" in instructions
        assert "Do not claim that an action was completed" in instructions

    def test_instructions_contain_privacy_boundaries(self):
        instructions = SHVYABaseInstructions.get()

        assert "private CRM information" in instructions
        assert "another organization" in instructions
        assert "another lead" in instructions

    def test_instructions_preserve_task_boundary(self):
        instructions = SHVYABaseInstructions.get()

        assert "The calling service determines the specific task" in instructions

    def test_empty_base_instructions_are_rejected(self):
        original = SHVYABaseInstructions.SYSTEM_INSTRUCTIONS

        try:
            SHVYABaseInstructions.SYSTEM_INSTRUCTIONS = ""

            with pytest.raises(
                BaseInstructionsError,
                match="cannot be empty",
            ):
                SHVYABaseInstructions.get()

        finally:
            SHVYABaseInstructions.SYSTEM_INSTRUCTIONS = original