from django.test import SimpleTestCase

from services.channels.template_cta_analytics_merge import (
    apply_confirmed_cta_authority,
)


class TemplateCTAAnalyticsMergeTests(SimpleTestCase):
    def test_confirmed_actions_replace_provider_url_click_duplicates(self):
        meta_results = {
            "meta-template": {
                "source": "meta",
                "days": [
                    {
                        "date": "2026-10-02",
                        "sent": 10,
                        "delivered": 8,
                        "read": 6,
                        "clicked": 4,
                    }
                ],
                "totals": {"sent": 10, "delivered": 8, "read": 6, "clicked": 4},
                "rates": {"delivered": 80.0, "read": 75.0, "clicked": 50.0},
                "availability": {"clicked": True},
                "clicks": [
                    {
                        "type": "url_button",
                        "button_content": "Call sales",
                        "count": 4,
                    }
                ],
            }
        }
        local_results = {
            "meta-template": {
                "click_scope": "all_tracked_buttons",
                "click_count_basis": "shvya_tracked_cta",
                "local_click_receipts_applied": True,
                "days": [
                    {
                        "date": "2026-10-02",
                        "sent": 10,
                        "delivered": 8,
                        "read": 6,
                        "clicked": 2,
                    }
                ],
                "clicks": [
                    {
                        "type": "phone_button",
                        "button_content": "Call sales",
                        "count": 2,
                    }
                ],
                "unique_clicks": [
                    {
                        "type": "phone_button",
                        "button_content": "Call sales",
                        "count": 1,
                    }
                ],
                "unique_click_total": 1,
            }
        }

        result = apply_confirmed_cta_authority(
            meta_results=meta_results,
            local_results=local_results,
        )["meta-template"]

        self.assertEqual(result["totals"]["clicked"], 2)
        self.assertEqual(result["clicks"], local_results["meta-template"]["clicks"])
        self.assertEqual(result["unique_click_total"], 1)
        self.assertEqual(result["click_source"], "shvya_cta_receipts")
        self.assertEqual(result["source"], "meta+shvya")
