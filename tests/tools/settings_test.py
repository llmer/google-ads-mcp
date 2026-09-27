# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for campaign settings, account tracking, campaign conversion goals and
campaign assets tools."""

import unittest

from fastmcp.exceptions import ToolError

from ads_mcp.tools import assets, campaigns, conversions, core
from tests.tools.ads_fakes import MutateToolTestCase


def _ops(operations, field, kind):
    return [
        getattr(getattr(op, field), kind)
        for op in operations
        if op._pb.WhichOneof("operation") == field
        and getattr(op, field)._pb.WhichOneof("operation") == kind
    ]


class TestUpdateCampaignSettings(MutateToolTestCase):

    def test_sets_presence_automation_and_devices(self):
        self.search_results = [
            [
                {
                    "campaign_criterion.resource_name": "customers/1/campaignCriteria/5~30000",
                    "campaign_criterion.device.type": "DESKTOP",
                }
            ]
        ]
        result = campaigns.update_campaign_settings(
            "1",
            5,
            location_targeting="PRESENCE",
            device_bid_adjustments={"desktop": -50, "TABLET": -30},
            text_asset_automation="OPTED_OUT",
            final_url_expansion="OPTED_OUT",
        )
        [operations] = self.mutate_calls()
        [campaign] = _ops(operations, "campaign_operation", "update")
        self.assertEqual(campaign.resource_name, "customers/1/campaigns/5")
        self.assertEqual(
            campaign.geo_target_type_setting.positive_geo_target_type.name, "PRESENCE"
        )
        self.assertEqual(
            {s.asset_automation_type.name: s.asset_automation_status.name
             for s in campaign.asset_automation_settings},
            {
                "TEXT_ASSET_AUTOMATION": "OPTED_OUT",
                "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION": "OPTED_OUT",
            },
        )
        [updated] = _ops(operations, "campaign_criterion_operation", "update")
        self.assertEqual(updated.resource_name, "customers/1/campaignCriteria/5~30000")
        self.assertAlmostEqual(updated.bid_modifier, 0.5)
        [created] = _ops(operations, "campaign_criterion_operation", "create")
        self.assertEqual(created.device.type_.name, "TABLET")
        self.assertAlmostEqual(created.bid_modifier, 0.7)
        self.assertEqual(result["changed"]["device_bid_adjustments"], {"DESKTOP": -50, "TABLET": -30})

    def test_rejects_out_of_range_adjustment_and_empty_call(self):
        with self.assertRaises(ToolError):
            campaigns.update_campaign_settings("1", 5, device_bid_adjustments={"MOBILE": 250})
        with self.assertRaises(ToolError):
            campaigns.update_campaign_settings("1", 5)


class TestUpdateAccountTracking(MutateToolTestCase):

    def test_updates_only_given_fields(self):
        core.update_account_tracking(
            "1", final_url_suffix="utm_source=google&utm_campaign={campaignid}",
            auto_tagging_enabled=True,
        )
        [operations] = self.mutate_calls()
        [op] = operations
        customer = op.customer_operation.update  # CustomerOperation is update-only
        self.assertEqual(customer.resource_name, "customers/1")
        self.assertEqual(customer.final_url_suffix, "utm_source=google&utm_campaign={campaignid}")
        self.assertTrue(customer.auto_tagging_enabled)
        self.assertEqual(
            list(operations[0].customer_operation.update_mask.paths),
            ["final_url_suffix", "auto_tagging_enabled"],
        )

    def test_rejects_leading_question_mark(self):
        with self.assertRaises(ToolError):
            core.update_account_tracking("1", final_url_suffix="?utm_source=x")


def _goal(category, origin, biddable):
    return {
        "campaign_conversion_goal.resource_name": f"rn/{category}~{origin}",
        "campaign_conversion_goal.category": category,
        "campaign_conversion_goal.origin": origin,
        "campaign_conversion_goal.biddable": biddable,
    }


class TestSetCampaignConversionGoals(MutateToolTestCase):

    def test_flips_only_goals_that_change(self):
        self.search_results = [[
            _goal("PURCHASE", "WEBSITE", False),
            _goal("DOWNLOAD", "APP", True),
            _goal("SIGNUP", "WEBSITE", False),
        ]]
        result = conversions.set_campaign_conversion_goals("1", 5, ["purchase:website"])
        [operations] = self.mutate_calls()
        updates = {g.resource_name: g.biddable for g in _ops(operations, "campaign_conversion_goal_operation", "update")}
        self.assertEqual(updates, {"rn/PURCHASE~WEBSITE": True, "rn/DOWNLOAD~APP": False})
        self.assertEqual(result["biddable"], ["PURCHASE:WEBSITE"])

    def test_refuses_unknown_goal(self):
        self.search_results = [[_goal("PURCHASE", "WEBSITE", True)]]
        with self.assertRaises(ToolError):
            conversions.set_campaign_conversion_goals("1", 5, ["LEAD:WEBSITE"])


class TestAddCampaignAssets(MutateToolTestCase):

    def test_creates_once_links_each_campaign_and_skips_existing(self):
        self.search_results = [[
            {"campaign.id": 6, "campaign_asset.field_type": "CALLOUT",
             "asset.callout_asset.callout_text": "No Subscription", "asset.id": 9},
        ]]
        result = assets.add_campaign_assets(
            "1",
            [5, 6],
            sitelinks=[assets.Sitelink(text="FAQ", final_url="https://example.com/faq",
                                       description1="How it works", description2="Pricing")],
            callouts=["No Subscription"],
            structured_snippet=assets.StructuredSnippet(header="Styles", values=["A", "B", "C"]),
            price=assets.PriceAsset(
                type="SERVICE_TIERS", currency_code="USD",
                items=[assets.PriceItem(header=f"Tier {i}", description="One-time",
                                        price=i * 5.0, final_url="https://example.com") for i in range(3)],
            ),
            business_name="Example",
            business_logo_asset_id=77,
        )
        [operations] = self.mutate_calls()
        created_assets = _ops(operations, "asset_operation", "create")
        self.assertEqual(len(created_assets), 5)  # sitelink, callout, snippet, price, name
        [price] = [a for a in created_assets if a.price_asset.price_offerings]
        self.assertEqual(price.price_asset.price_offerings[1].price.amount_micros, 5_000_000)
        links = _ops(operations, "campaign_asset_operation", "create")
        pairs = {(l.campaign, l.field_type.name) for l in links}
        self.assertIn(("customers/1/campaigns/5", "CALLOUT"), pairs)
        self.assertNotIn(("customers/1/campaigns/6", "CALLOUT"), pairs)
        self.assertIn(("customers/1/campaigns/6", "BUSINESS_LOGO"), pairs)
        self.assertEqual(len(links), 11)
        self.assertIn("CALLOUT: No Subscription", result["campaigns"]["6"]["skipped"])
        # Newly created assets are linked through their temporary resource names.
        temp_names = {a.resource_name for a in created_assets}
        self.assertTrue(all(n.startswith("customers/1/assets/-") for n in temp_names))

    def test_validates_lengths_and_price_items(self):
        with self.assertRaises(ToolError):
            assets.add_campaign_assets("1", [5], callouts=["x" * 26])
        with self.assertRaises(ToolError):
            assets.add_campaign_assets(
                "1", [5],
                structured_snippet=assets.StructuredSnippet(header="Colors", values=["a", "b", "c"]),
            )
        with self.assertRaises(ToolError):
            assets.add_campaign_assets(
                "1", [5],
                price=assets.PriceAsset(type="SERVICES", currency_code="USD", items=[]),
            )


if __name__ == "__main__":
    unittest.main()


class TestSetAutoApplyRecommendations(MutateToolTestCase):

    def test_pauses_enabled_subscriptions_and_counts_unknown(self):
        def sub(rtype, status):
            return {
                "recommendation_subscription.resource_name": f"customers/1/recommendationSubscriptions/{rtype}",
                "recommendation_subscription.type": rtype,
                "recommendation_subscription.status": status,
            }
        self.search_results = [[
            sub("USE_BROAD_MATCH_KEYWORD", "ENABLED"),
            sub("KEYWORD", "PAUSED"),
            sub("UNKNOWN", "ENABLED"),
        ]]
        result = campaigns.set_auto_apply_recommendations("1")
        [operations] = self.mutate_calls()
        updates = _ops(operations, "recommendation_subscription_operation", "update")
        self.assertEqual([u.status.name for u in updates], ["PAUSED"])
        self.assertEqual(result["changed"], ["USE_BROAD_MATCH_KEYWORD"])
        self.assertEqual(result["unsupported_unknown_types"], 1)
