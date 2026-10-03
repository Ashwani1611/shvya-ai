"""Credential-free regressions; no provider calls, customer data or sends."""
import json
import unittest
from copy import deepcopy
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import Mock

from apps.ai_engagement.services import conversation_continuity_runtime as continuity


@dataclass(frozen=True)
class Context:
    conversation: dict
    lead: dict = field(default_factory=dict)
    organization: dict = field(default_factory=dict)
    stage: dict = field(default_factory=dict)


def inbound(body, source="in"):
    return {"id": source, "direction": "inbound", "body": body, "status": "received"}


def outbound(body="Reply", status="sent"):
    return {"id": "out", "direction": "outbound", "body": body, "status": status}


def context(*messages):
    return Context({"channel": "whatsapp", "messages": list(messages)})


def classify(body):
    return "request" if any(word in body.lower() for word in ("pricing", "brochure", "?")) else "none"


class PendingTurnContractTests(unittest.TestCase):
    def test_pricing_survives_repeated_brochure_requests(self):
        ctx = context(outbound("Qualification complete"), inbound("Yes", "yes"),
                      inbound("What is the product and its pricing and package", "pricing"),
                      *[inbound("Share product brochure", f"brochure-{n}") for n in range(6)])
        pending = continuity.pending_inbound(ctx)
        self.assertEqual([m["id"] for _, m in pending], ["yes", "pricing", "brochure-5"])
        query = continuity._wrap_query(lambda self, **kwargs: "base")(None, context=ctx)
        self.assertIn("pricing and package", query)
        self.assertIn("Share product brochure", query)
        self.assertLessEqual(len(query), continuity.MAX_QUERY_CHARS)

    def test_inflight_and_failed_replies_do_not_erase_question(self):
        for status in ("queued", "pending", "sending", "failed", "cancelled"):
            with self.subTest(status=status):
                ctx = context(inbound("pricing", "a"), outbound(status=status), inbound("please", "b"))
                self.assertEqual(len(continuity.pending_inbound(ctx)), 2)

    def test_visible_reply_is_conservative_boundary(self):
        for status in ("sent", "delivered", "read", ""):
            with self.subTest(status=status):
                ctx = context(inbound("old price", "a"), outbound(status=status), inbound("new question", "b"))
                self.assertEqual([m["id"] for _, m in continuity.pending_inbound(ctx)], ["b"])

    def test_burst_after_qualification_keeps_exact_customer_values(self):
        ctx = context(inbound("28", "volume"), outbound("Are you running ads?"), inbound("No", "ads"),
                      outbound("Thank you"), inbound("pricing", "price"))
        self.assertEqual([m["body"] for _, m in continuity.pending_inbound(ctx)], ["pricing"])
        self.assertEqual(ctx.conversation["messages"][0]["body"], "28")

    def test_duplicate_text_compacts_context_not_source_events(self):
        ctx = context(inbound("Brochure", "a"), inbound("  brochure  ", "b"))
        before = deepcopy(ctx)
        self.assertEqual(continuity.pending_inbound(ctx)[0][1]["id"], "b")
        self.assertEqual(ctx, before)
        self.assertEqual(len(ctx.conversation["messages"]), 2)

    def test_bounded_window_preserves_early_and_latest_messages(self):
        ctx = context(*[inbound(f"request {n}", str(n)) for n in range(40)])
        ids = [m["id"] for _, m in continuity.pending_inbound(ctx)]
        self.assertEqual(len(ids), continuity.MAX_PENDING)
        self.assertEqual(ids[0], "0")
        self.assertEqual(ids[-1], "39")

    def test_invalid_message_shapes_are_ignored(self):
        ctx = context(None, "bad", {}, {"direction": "inbound", "body": {}}, inbound("valid"))
        self.assertEqual(len(continuity.pending_inbound(ctx)), 1)
        self.assertEqual(continuity.pending_inbound(SimpleNamespace(conversation={"messages": {}})), [])

    def test_prompt_and_query_budgets(self):
        ctx = context(*[inbound(f"{n}:" + "x" * 10000 + f"latest-{n}", str(n)) for n in range(20)])
        payload = continuity.turn_payload(ctx)
        self.assertLessEqual(sum(len(m["body"]) for m in payload["messages"]), continuity.MAX_PENDING_CHARS)
        self.assertTrue(all(m["truncated"] for m in payload["messages"]))
        self.assertFalse(payload["authorizes_actions"])
        query = continuity._wrap_query(lambda self, **kwargs: "base")(None, context=ctx)
        self.assertLessEqual(len(query), continuity.MAX_QUERY_CHARS)
        self.assertIn("latest-19", query)

    def test_input_keeps_authoritative_source_and_qualification(self):
        ctx = context(inbound("pricing", "old"), inbound("28", "latest"))
        original = Mock(return_value=json.dumps({"qualification_turn": {"source_message_id": "latest"}}))
        result = json.loads(continuity._wrap_input(original)(None, context=ctx))
        self.assertEqual(result["qualification_turn"]["source_message_id"], "latest")
        self.assertEqual([m["source_message_id"] for m in result["unanswered_customer_turn"]["messages"]], ["old", "latest"])
        self.assertEqual(original.call_count, 1)

    def test_single_input_preserves_existing_payload(self):
        raw = '{"existing":true}'
        wrapped = continuity._wrap_input(lambda self, **kwargs: raw)
        self.assertEqual(wrapped(None, context=context(inbound("hello"))), raw)

    def test_retrieval_looks_back_past_a_nudge(self):
        def original(self, *, context):
            return "pricing" in context.conversation["messages"][-1]["body"]
        ctx = context(inbound("pricing", "a"), inbound("please", "b"))
        self.assertTrue(continuity._wrap_should_retrieve(original)(None, context=ctx))
        self.assertEqual(ctx.conversation["messages"][-1]["id"], "b")

    def test_only_numeric_answers_do_not_force_retrieval(self):
        self.assertFalse(continuity._wrap_should_retrieve(lambda self, **kwargs: False)(
            None, context=context(inbound("28"), inbound("No"))))

    def test_short_affirmative_burst_keeps_original_offer_context(self):
        ctx = context(outbound("Would you like the brochure?"), inbound("yes", "a"), inbound("please", "b"))
        def original(self, **kwargs):
            return "Lead: yes\nPrevious SHVYA context: Would you like the brochure?"
        query = continuity._wrap_query(original)(None, context=ctx)
        self.assertIn("Would you like the brochure?", query)
        self.assertLessEqual(len(query), continuity.MAX_QUERY_CHARS)

    def test_namespace_context_view_does_not_mutate_original(self):
        ctx = SimpleNamespace(conversation={"messages": [inbound("pricing"), inbound("please")]})
        view = continuity._context_at(ctx, 0)
        self.assertEqual(len(view.conversation["messages"]), 1)
        self.assertEqual(len(ctx.conversation["messages"]), 2)

    def test_single_query_is_unchanged(self):
        wrapped = continuity._wrap_query(lambda self, **kwargs: "canonical-query")
        self.assertEqual(wrapped(None, context=context(inbound("price"))), "canonical-query")


class RoutingContractTests(unittest.TestCase):
    def test_extracted_answer_does_not_hide_information_request(self):
        ctx = context(inbound("What is pricing?", "q"), inbound("No", "answer"))
        service = SimpleNamespace(
            _should_retrieve_knowledge=lambda **kwargs: True,
            _build_knowledge_query=lambda **kwargs: "pricing",
        )
        state = {"context": ctx, "answer_extracted": True, "service": service}
        routed = continuity._wrap_route(lambda state: {"route": "generate"}, classify)(state)
        self.assertEqual(routed, {"route": "rag", "retrieval_query": "pricing"})
        self.assertEqual(state["context"].conversation["messages"][-1]["id"], "answer")

    def test_mixed_final_answer_and_pricing_request_retrieves(self):
        ctx = context(inbound("No, can you explain pricing?", "answer"))
        service = SimpleNamespace(_should_retrieve_knowledge=lambda **kwargs: True,
                                  _build_knowledge_query=lambda **kwargs: "pricing")
        result = continuity._wrap_route(lambda state: {"route": "generate"}, classify)(
            {"context": ctx, "answer_extracted": True, "service": service})
        self.assertEqual(result["route"], "rag")

    def test_preview_context_does_not_start_unrequested_live_retrieval(self):
        wrapped = continuity._wrap_route(lambda state: {"route": "generate"}, classify)
        state = {"context": context(inbound("pricing")), "answer_extracted": True, "caller_supplied_context": True}
        self.assertEqual(wrapped(state), {"route": "generate"})

    def test_rag_and_direct_routes_are_preserved(self):
        for route in ("rag", "direct"):
            wrapped = continuity._wrap_route(lambda state: {"route": route}, classify)
            self.assertEqual(wrapped({}), {"route": route})

    def test_extraction_keeps_state_but_drops_question_only_shortcut(self):
        update = {"direct_decision": "next-question", "qualification_state": {"answer": "No"}, "answer_extracted": True}
        wrapped = continuity._wrap_extract(lambda state: update, classify)
        result = wrapped({"context": context(inbound("pricing?"), inbound("No"))})
        self.assertNotIn("direct_decision", result)
        self.assertTrue(result["answer_extracted"])
        self.assertEqual(result["qualification_state"], {"answer": "No"})
        self.assertIn("direct_decision", update)

    def test_plain_qualification_shortcut_is_unchanged(self):
        update = {"direct_decision": "next-question", "answer_extracted": True}
        result = continuity._wrap_extract(lambda state: update, classify)({"context": context(inbound("No"))})
        self.assertEqual(result, update)


class FileCandidateContractTests(unittest.TestCase):
    def test_brochure_followed_by_please_keeps_candidate(self):
        calls = []
        def original(self, *, organization, context):
            calls.append((organization, context))
            return [{"document_id": 7, "share_instruction": "Only when permitted"}] if "brochure" in context.conversation["messages"][-1]["body"] else []
        ctx = context(inbound("send brochure", "a"), inbound("please", "b"))
        before = deepcopy(ctx)
        org = object()
        result = continuity._wrap_candidates(original)(None, organization=org, context=ctx)
        self.assertEqual(result[0]["document_id"], 7)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(o is org for o, _ in calls))
        self.assertEqual(ctx, before)
        self.assertEqual(result[0]["share_instruction"], "Only when permitted")

    def test_latest_explicit_request_does_not_double_lookup(self):
        original = Mock(return_value=[{"document_id": 7}])
        ctx = context(inbound("pricing", "a"), inbound("send brochure", "b"))
        continuity._wrap_candidates(original)(None, organization=object(), context=ctx)
        self.assertEqual(original.call_count, 1)

    def test_cancellation_and_topic_change_do_not_reintroduce_candidates(self):
        for body in ("Do not send it", "stop", "Instead book a call", "I changed my mind"):
            with self.subTest(body=body):
                original = Mock(return_value=[])
                ctx = context(inbound("send brochure", "a"), inbound(body, "b"))
                self.assertEqual(continuity._wrap_candidates(original)(None, organization=object(), context=ctx), [])
                self.assertEqual(original.call_count, 1)

    def test_old_answered_file_request_is_not_reopened(self):
        original = Mock(return_value=[])
        ctx = context(inbound("brochure"), outbound(), inbound("please"))
        continuity._wrap_candidates(original)(None, organization=object(), context=ctx)
        self.assertEqual(original.call_count, 1)

    def test_candidate_union_is_bounded_and_unique(self):
        original = Mock(side_effect=[[{"document_id": n} for n in range(10)], [{"document_id": n} for n in range(5, 15)]])
        result = continuity._wrap_candidates(original)(None, organization=object(),
                    context=context(inbound("brochure", "a"), inbound("please", "b")))
        self.assertEqual(len(result), 10)
        self.assertEqual(len({c["document_id"] for c in result}), 10)
        self.assertEqual(result[0]["document_id"], 5)


INTRO = ("Hi! I'm an AI assistant for Example CRM. We help businesses manage leads, "
         "automate follow-ups, and respond to enquiries more efficiently. "
         "I'd like to understand your current setup with a few quick questions.")


class ReplyPolicyContractTests(unittest.TestCase):
    def test_repeated_full_intro_enters_existing_repair_path(self):
        wrapped = continuity._wrap_policy(lambda self, **kwargs: None, ValueError)
        with self.assertRaisesRegex(ValueError, "already sent"):
            wrapped(None, decision=SimpleNamespace(message=INTRO), context=context(outbound(INTRO), inbound("Hello")))

    def test_punctuation_variant_is_still_repeated_intro(self):
        wrapped = continuity._wrap_policy(lambda self, **kwargs: None, ValueError)
        with self.assertRaises(ValueError):
            wrapped(None, decision=SimpleNamespace(message=INTRO.replace("follow-ups,", "follow-ups")),
                    context=context(outbound(INTRO), inbound("hello")))

    def test_first_intro_is_allowed(self):
        continuity._wrap_policy(lambda self, **kwargs: None, ValueError)(None,
            decision=SimpleNamespace(message=INTRO), context=context(inbound("Hello")))

    def test_failed_intro_is_not_claimed_as_previously_sent(self):
        continuity._wrap_policy(lambda self, **kwargs: None, ValueError)(None,
            decision=SimpleNamespace(message=INTRO), context=context(outbound(INTRO, "failed"), inbound("Hello")))

    def test_repeated_prices_and_brief_greetings_remain_allowed(self):
        for message in ("Hello!", "The approved plan costs X." * 12):
            continuity._wrap_policy(lambda self, **kwargs: None, ValueError)(None,
                decision=SimpleNamespace(message=message), context=context(outbound(message), inbound("Repeat it")))

    def test_existing_permission_or_silence_rejection_is_not_bypassed(self):
        original = Mock(side_effect=ValueError("existing silence guard"))
        with self.assertRaisesRegex(ValueError, "existing silence"):
            continuity._wrap_policy(original, ValueError)(None, decision=SimpleNamespace(message=""), context=context(inbound("hi")))

    def test_instructions_keep_existing_authority_and_limits(self):
        result = continuity._wrap_instructions(lambda self, **kwargs: "Existing authority")(None, context=context())
        self.assertTrue(result.startswith("Existing authority"))
        self.assertIn("untrusted conversation", result)
        self.assertIn("does not change the current qualification requirement", result)


if __name__ == "__main__":
    unittest.main()
