from django.test import SimpleTestCase

from apps.ai_engagement.services.crm_actions import (
    CRMActionSchemaError,
    validate_crm_actions,
)


class CRMActionSchemaTests(SimpleTestCase):
    # ============================================================
    # EMPTY
    # ============================================================

    def test_accepts_empty_actions(self):
        self.assertEqual(
            validate_crm_actions([]),
            [],
        )

    # ============================================================
    # ATTRIBUTE UPDATES
    # ============================================================

    def test_accepts_attribute_updates(self):
        actions = validate_crm_actions(
            [
                {
                    "type": "attribute_updates",
                    "updates": [
                        {
                            "key": "budget",
                            "value": "50000",
                        }
                    ],
                }
            ]
        )

        self.assertEqual(
            actions[0]["type"],
            "attribute_updates",
        )

        self.assertEqual(
            actions[0]["updates"][0]["key"],
            "budget",
        )

        self.assertEqual(
            actions[0]["updates"][0]["value"],
            "50000",
        )

    def test_rejects_attribute_updates_without_key(self):
        with self.assertRaises(
            CRMActionSchemaError,
        ):
            validate_crm_actions(
                [
                    {
                        "type": "attribute_updates",
                        "updates": [
                            {
                                "value": "50000",
                            }
                        ],
                    }
                ]
            )

    def test_rejects_attribute_updates_with_extra_key(self):
        with self.assertRaises(
            CRMActionSchemaError,
        ):
            validate_crm_actions(
                [
                    {
                        "type": "attribute_updates",
                        "updates": [
                            {
                                "key": "budget",
                                "value": "50000",
                                "extra": "not allowed",
                            }
                        ],
                    }
                ]
            )

    # ============================================================
    # PIPELINE TRANSITION
    # ============================================================

    def test_accepts_pipeline_transition(self):
        stage_id = (
            "550e8400-e29b-41d4-a716-446655440000"
        )

        actions = validate_crm_actions(
            [
                {
                    "type": "pipeline_transition",
                    "stage_shift": {
                        "stage_id": stage_id,
                    },
                }
            ]
        )

        self.assertEqual(
            actions[0]["type"],
            "pipeline_transition",
        )

        self.assertEqual(
            actions[0]["stage_shift"]["stage_id"],
            stage_id,
        )

    def test_accepts_non_uuid_stage_identifier(self):
        """
        Schema validation only requires a non-empty identifier.

        Actual CRM ownership/existence validation belongs to 10.4.
        """

        actions = validate_crm_actions(
            [
                {
                    "type": "pipeline_transition",
                    "stage_shift": {
                        "stage_id": "existing-stage-id",
                    },
                }
            ]
        )

        self.assertEqual(
            actions[0]["stage_shift"]["stage_id"],
            "existing-stage-id",
        )

    def test_rejects_empty_stage_identifier(self):
        with self.assertRaises(
            CRMActionSchemaError,
        ):
            validate_crm_actions(
                [
                    {
                        "type": "pipeline_transition",
                        "stage_shift": {
                            "stage_id": "",
                        },
                    }
                ]
            )

    def test_rejects_invalid_pipeline_transition_schema(self):
        with self.assertRaises(
            CRMActionSchemaError,
        ):
            validate_crm_actions(
                [
                    {
                        "type": "pipeline_transition",
                        "stage_shift": {
                            "stage_id": "stage-id",
                            "extra": "not allowed",
                        },
                    }
                ]
            )

    # ============================================================
    # ADD NOTE
    # ============================================================

    def test_accepts_add_note(self):
        actions = validate_crm_actions(
            [
                {
                    "type": "add_note",
                    "note": "Lead confirmed interest.",
                }
            ]
        )

        self.assertEqual(
            actions[0]["type"],
            "add_note",
        )

        self.assertEqual(
            actions[0]["note"],
            "Lead confirmed interest.",
        )

    def test_rejects_empty_note(self):
        with self.assertRaises(
            CRMActionSchemaError,
        ):
            validate_crm_actions(
                [
                    {
                        "type": "add_note",
                        "note": "",
                    }
                ]
            )

    def test_rejects_extra_note_field(self):
        with self.assertRaises(
            CRMActionSchemaError,
        ):
            validate_crm_actions(
                [
                    {
                        "type": "add_note",
                        "note": "Useful note.",
                        "internal_reasoning": "secret",
                    }
                ]
            )

    # ============================================================
    # REMINDER
    # ============================================================

    def test_accepts_create_reminder(self):
        actions = validate_crm_actions(
            [
                {
                    "type": "create_reminder",
                    "title": "Follow up with lead",
                    "description": "Discuss enrollment.",
                    "due_at": (
                        "2026-09-05T10:00:00+05:30"
                    ),
                }
            ]
        )

        self.assertEqual(
            actions[0]["type"],
            "create_reminder",
        )

        self.assertEqual(
            actions[0]["title"],
            "Follow up with lead",
        )

    def test_accepts_empty_reminder_description(self):
        actions = validate_crm_actions(
            [
                {
                    "type": "create_reminder",
                    "title": "Follow up",
                    "description": "",
                    "due_at": (
                        "2026-09-05T10:00:00+05:30"
                    ),
                }
            ]
        )

        self.assertEqual(
            actions[0]["description"],
            "",
        )

    def test_rejects_empty_reminder_title(self):
        with self.assertRaises(
            CRMActionSchemaError,
        ):
            validate_crm_actions(
                [
                    {
                        "type": "create_reminder",
                        "title": "",
                        "description": "Follow up.",
                        "due_at": (
                            "2026-09-05T10:00:00+05:30"
                        ),
                    }
                ]
            )

    def test_rejects_invalid_reminder_datetime(self):
        with self.assertRaises(
            CRMActionSchemaError,
        ):
            validate_crm_actions(
                [
                    {
                        "type": "create_reminder",
                        "title": "Follow up",
                        "description": "Discuss enrollment.",
                        "due_at": "tomorrow",
                    }
                ]
            )

    # ============================================================
    # CONTACT UPDATES
    # ============================================================

    def test_accepts_contact_updates(self):
        actions = validate_crm_actions(
            [
                {
                    "type": "contact_updates",
                    "updates": [
                        {
                            "contact_id": "existing-contact-id",
                            "channel": "whatsapp",
                            "handle": "+919999999999",
                        }
                    ],
                }
            ]
        )

        self.assertEqual(
            actions[0]["type"],
            "contact_updates",
        )

        self.assertEqual(
            actions[0]["updates"][0]["contact_id"],
            "existing-contact-id",
        )

        self.assertEqual(
            actions[0]["updates"][0]["channel"],
            "whatsapp",
        )

        self.assertEqual(
            actions[0]["updates"][0]["handle"],
            "+919999999999",
        )

    def test_rejects_empty_contact_id(self):
        with self.assertRaises(
            CRMActionSchemaError,
        ):
            validate_crm_actions(
                [
                    {
                        "type": "contact_updates",
                        "updates": [
                            {
                                "contact_id": "",
                                "channel": "whatsapp",
                                "handle": "+919999999999",
                            }
                        ],
                    }
                ]
            )

    def test_rejects_extra_contact_update_field(self):
        with self.assertRaises(
            CRMActionSchemaError,
        ):
            validate_crm_actions(
                [
                    {
                        "type": "contact_updates",
                        "updates": [
                            {
                                "contact_id": "contact-id",
                                "channel": "whatsapp",
                                "handle": "+919999999999",
                                "extra": "not allowed",
                            }
                        ],
                    }
                ]
            )

    # ============================================================
    # GENERAL SCHEMA VALIDATION
    # ============================================================

    def test_rejects_non_list_actions(self):
        with self.assertRaises(
            CRMActionSchemaError,
        ):
            validate_crm_actions(
                {}
            )

    def test_rejects_non_object_action(self):
        with self.assertRaises(
            CRMActionSchemaError,
        ):
            validate_crm_actions(
                [
                    "invalid action",
                ]
            )

    def test_rejects_missing_action_type(self):
        with self.assertRaises(
            CRMActionSchemaError,
        ):
            validate_crm_actions(
                [
                    {
                        "note": "Useful note.",
                    }
                ]
            )

    def test_rejects_unknown_action_type(self):
        with self.assertRaises(
            CRMActionSchemaError,
        ):
            validate_crm_actions(
                [
                    {
                        "type": "delete_lead",
                    }
                ]
            )

    def test_rejects_unknown_action_fields(self):
        with self.assertRaises(
            CRMActionSchemaError,
        ):
            validate_crm_actions(
                [
                    {
                        "type": "add_note",
                        "note": "Useful note.",
                        "internal_reasoning": "secret",
                    }
                ]
            )

    # ============================================================
    # MULTIPLE ACTIONS
    # ============================================================

    def test_accepts_multiple_actions(self):
        actions = validate_crm_actions(
            [
                {
                    "type": "attribute_updates",
                    "updates": [
                        {
                            "key": "budget",
                            "value": "50000",
                        }
                    ],
                },
                {
                    "type": "add_note",
                    "note": "Lead supplied budget.",
                },
                {
                    "type": "create_reminder",
                    "title": "Follow up",
                    "description": "Discuss budget.",
                    "due_at": (
                        "2026-09-05T10:00:00+05:30"
                    ),
                },
            ]
        )

        self.assertEqual(
            len(actions),
            3,
        )

        self.assertEqual(
            [
                action["type"]
                for action in actions
            ],
            [
                "attribute_updates",
                "add_note",
                "create_reminder",
            ],
        )