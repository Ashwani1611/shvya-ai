import json
from types import SimpleNamespace
from unittest.mock import Mock

from django.test import SimpleTestCase, override_settings

from apps.ai_engagement.prompts.engagement import CUSTOMER_ENGAGEMENT_INSTRUCTIONS
from apps.ai_engagement.services.ai_provider import AITextResult, OpenAIProvider
from apps.ai_engagement.services.engagement import ENGAGEMENT_RESPONSE_SCHEMA
from apps.ai_engagement.services.playground import PlaygroundService


@override_settings(OPENAI_API_KEY="test-key")
class EngagementStructuredOutputContractTests(SimpleTestCase):
    def test_engagement_schema_is_forced_strict_with_closed_nested_objects(self):
        provider = OpenAIProvider(client=Mock())
        config = provider._structured_text_config(ENGAGEMENT_RESPONSE_SCHEMA)
        response_format = config["format"]

        self.assertTrue(response_format["strict"])
        schema = response_format["schema"]
        self.assertFalse(schema["additionalProperties"])

        crm_items = schema["properties"]["crm_actions"]["items"]["anyOf"]
        self.assertEqual(len(crm_items), 5)
        for action_schema in crm_items:
            self.assertFalse(action_schema["additionalProperties"])
            self.assertEqual(
                set(action_schema["required"]),
                set(action_schema["properties"]),
            )

        qualification_item = schema["properties"]["qualification_updates"]["items"]
        self.assertFalse(qualification_item["additionalProperties"])
        self.assertEqual(
            set(qualification_item["required"]),
            set(qualification_item["properties"]),
        )

    def test_short_greetings_are_explicitly_customer_facing(self):
        self.assertIn(
            'greetings such as "hi"/"hello"',
            CUSTOMER_ENGAGEMENT_INSTRUCTIONS,
        )
        self.assertIn(
            "Do not use NO_ACTION merely because the message is",
            CUSTOMER_ENGAGEMENT_INSTRUCTIONS,
        )


class PlaygroundStructuredOutputTests(SimpleTestCase):
    def test_playground_uses_same_engagement_response_schema_as_production(self):
        provider = Mock()
        provider.generate_text.return_value = AITextResult(
            json.dumps(
                {
                    "should_engage": True,
                    "message": "Hello! How can I help?",
                    "file_document_id": None,
                    "crm_actions": [],
                    "qualification_updates": [],
                    "next_requirement_id": None,
                    "reason_code": "NORMAL_CONVERSATION",
                }
            ),
            "test-model",
        )
        org_info_service = Mock()
        org_info_service.get_or_create.return_value = SimpleNamespace(
            ai_enabled=True,
            about="Test organization",
            bot_languages="English",
            qualification_requirements="",
            engagement_instructions="",
            bump_up_enabled=False,
            bump_up_count=0,
        )
        embedding_service = Mock()
        embedding_service.embed_text.return_value = [0.1, 0.2]
        retrieval_service = Mock()
        retrieval_service.retrieve_by_vector.return_value = []

        service = PlaygroundService(
            provider=provider,
            org_info_service=org_info_service,
            embedding_service=embedding_service,
            retrieval_service=retrieval_service,
        )
        result = service.run(
            organization=SimpleNamespace(id="org-1", name="Test Org"),
            session_id="session-1",
            message="Hi",
            history=[],
        )

        self.assertTrue(result.should_engage)
        self.assertEqual(result.response, "Hello! How can I help?")
        kwargs = provider.generate_text.call_args.kwargs
        self.assertEqual(kwargs["response_schema"], ENGAGEMENT_RESPONSE_SCHEMA)
        self.assertEqual(kwargs["metadata"]["task"], "playground")
        self.assertEqual(kwargs["metadata"]["phase"], "primary")
