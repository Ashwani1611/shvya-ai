"""Complete account reviews must be able to read beyond the first page."""

from apps.ai_engagement.models import FAQ
from apps.followups.touchpoint_models import TouchpointCategory, TouchpointReply
from apps.integrations.models import OperationsPolicy
from apps.integrations.operations_policy import CAP_ORGANIZATION_READ, ROLE_ORGANIZATION_ADMIN
from apps.integrations.tests.operations_mcp_test_base import OperationsMCPBase


class OperationsContentPaginationTests(OperationsMCPBase):
    def setUp(self):
        super().setUp()
        OperationsPolicy.objects.create(organization=self.organization, organization_admin_enabled=True,
                                        allowed_capabilities=[CAP_ORGANIZATION_READ])
        self.bearer = self._token(actor=self.admin, role=ROLE_ORGANIZATION_ADMIN, organization=self.organization)

    def _ok(self, name, arguments=None):
        result = self._result(self._call(self.bearer, name, arguments))
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def _error(self, name, arguments):
        result = self._result(self._call(self.bearer, name, arguments))
        self.assertTrue(result["isError"], result)

    def test_faqs_beyond_first_hundred_are_accessible_and_scoped(self):
        FAQ.objects.bulk_create([FAQ(organization=self.organization, question=f"Question {index}", answer=f"Answer {index}") for index in range(103)])
        FAQ.objects.create(organization=self.other_organization, question="Other tenant", answer="Not visible")
        first = self._ok("list_faqs")
        self.assertEqual(first["count"], 100)
        self.assertTrue(first["has_more"])
        next_page = self._ok("list_faqs", {"offset": first["next_offset"]})
        self.assertEqual(next_page["count"], 3)
        self.assertFalse(next_page["has_more"])
        self.assertIsNone(next_page["next_offset"])
        self.assertEqual(len({row["id"] for row in first["faqs"] + next_page["faqs"]}), 103)

    def test_faq_pagination_applies_active_filter_before_slicing(self):
        FAQ.objects.bulk_create([FAQ(organization=self.organization, question=f"Question {index}", answer="Answer", is_active=index % 2 == 0) for index in range(5)])
        first = self._ok("list_faqs", {"active_only": True, "limit": 2})
        second = self._ok("list_faqs", {"active_only": True, "limit": 2, "offset": first["next_offset"]})
        self.assertEqual(first["count"] + second["count"], 3)
        self.assertTrue(all(row["active"] for row in first["faqs"] + second["faqs"]))

    def test_saved_replies_page_both_categories_and_each_category(self):
        categories = [TouchpointCategory(organization=self.organization, name=f"Category {index:02}") for index in range(22)]
        TouchpointCategory.objects.bulk_create(categories)
        TouchpointReply.objects.bulk_create([TouchpointReply(category=categories[0], title=f"Reply {index:03}", body="Approved text") for index in range(106)])
        first = self._ok("list_touchpoints")
        self.assertEqual(first["category_count"], 20)
        self.assertEqual(len(first["categories"][0]["replies"]), 25)
        self.assertEqual(first["next_category_offset"], 20)
        self.assertEqual(first["categories"][0]["next_reply_offset"], 25)
        next_categories = self._ok("list_touchpoints", {"category_offset": first["next_category_offset"]})
        self.assertEqual(next_categories["category_count"], 2)
        self.assertFalse(next_categories["has_more"])
        category_id = first["categories"][0]["category_id"]
        remaining = self._ok("list_touchpoints", {"category_id": category_id, "reply_offset": 25, "reply_limit": 100})
        self.assertEqual(remaining["count"], 81)
        self.assertFalse(remaining["categories"][0]["has_more"])
        self.assertIsNone(remaining["categories"][0]["next_reply_offset"])
        self.assertEqual(len({reply["id"] for reply in first["categories"][0]["replies"] + remaining["categories"][0]["replies"]}), 106)

    def test_saved_reply_filter_and_foreign_category_protection(self):
        category = TouchpointCategory.objects.create(organization=self.organization, name="Own")
        other = TouchpointCategory.objects.create(organization=self.other_organization, name="Other")
        TouchpointReply.objects.create(category=other, title="Private", body="Other tenant content")
        TouchpointReply.objects.create(category=category, title="Active", body="Available")
        TouchpointReply.objects.create(category=category, title="Old", body="Archived", is_active=False)
        self.assertEqual(self._ok("list_touchpoints")["count"], 1)
        self.assertEqual(self._ok("list_touchpoints", {"include_archived": True})["count"], 2)
        self._error("list_touchpoints", {"category_id": str(other.pk)})

    def test_pagination_bounds_are_strict(self):
        for args in ({"limit": 101}, {"offset": -1}, {"limit": True}, {"active_only": "true"}):
            self._error("list_faqs", args)
        for args in ({"category_limit": 21}, {"reply_limit": 101}, {"reply_offset": 1}, {"category_offset": -1}):
            self._error("list_touchpoints", args)
