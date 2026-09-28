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
            campaign.geo_target_type_setting.positive_geo_target_type.name,
            "PRESENCE",
        )
        self.assertEqual(
            {
                s.asset_automation_type.name: s.asset_automation_status.name
                for s in campaign.asset_automation_settings
            },
            {
                "TEXT_ASSET_AUTOMATION": "OPTED_OUT",
                "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION": "OPTED_OUT",
            },
        )
        [updated] = _ops(operations, "campaign_criterion_operation", "update")
        self.assertEqual(
            updated.resource_name, "customers/1/campaignCriteria/5~30000"
        )
        self.assertAlmostEqual(updated.bid_modifier, 0.5)
        [created] = _ops(operations, "campaign_criterion_operation", "create")
        self.assertEqual(created.device.type_.name, "TABLET")
        self.assertAlmostEqual(created.bid_modifier, 0.7)
        self.assertEqual(
            result["changed"]["device_bid_adjustments"],
            {"DESKTOP": -50, "TABLET": -30},
        )

    def test_rejects_out_of_range_adjustment_and_empty_call(self):
        with self.assertRaises(ToolError):
            campaigns.update_campaign_settings(
                "1", 5, device_bid_adjustments={"MOBILE": 250}
            )
        with self.assertRaises(ToolError):
            campaigns.update_campaign_settings("1", 5)

    def _campaign(self, status="ENABLED", serving_status="SERVING"):
        return [
            {
                "campaign.status": status,
                "campaign.serving_status": serving_status,
                "campaign.end_date_time": "2026-10-31 23:59:59",
            }
        ]

    def test_sets_name_networks_and_end_date(self):
        self.search_results = [self._campaign()]
        result = campaigns.update_campaign_settings(
            "1",
            5,
            name=" Search - Brand ",
            target_google_search=True,
            target_search_network=False,
            target_content_network=False,
            end_date="2026-12-31",
        )
        [[op]] = self.mutate_calls()
        campaign = op.campaign_operation.update
        self.assertEqual(campaign.resource_name, "customers/1/campaigns/5")
        self.assertEqual(campaign.name, "Search - Brand")
        self.assertTrue(campaign.network_settings.target_google_search)
        self.assertFalse(campaign.network_settings.target_search_network)
        # False is sent explicitly, not left unset.
        self.assertIn("target_content_network", campaign.network_settings)
        self.assertEqual(campaign.end_date_time, "2026-12-31 23:59:59")
        self.assertEqual(
            list(op.campaign_operation.update_mask.paths),
            [
                "name",
                "network_settings.target_google_search",
                "network_settings.target_search_network",
                "network_settings.target_content_network",
                "end_date_time",
            ],
        )
        # Only the campaign's status is read, for the end date.
        [query] = self.queries
        self.assertIn("campaign.serving_status", query)
        self.assertEqual(result["changed"]["end_date"], "2026-12-31")
        self.assertEqual(
            result["previous_end_date_time"], "2026-10-31 23:59:59"
        )
        self.assertFalse(result["validate_only"])

    def test_other_settings_read_nothing(self):
        campaigns.update_campaign_settings("1", 5, name="Search - Generic")
        self.assertEqual(self.queries, [])
        [[op]] = self.mutate_calls()
        self.assertEqual(
            list(op.campaign_operation.update_mask.paths), ["name"]
        )

    def test_refuses_to_restart_an_ended_enabled_campaign(self):
        for kwargs in [{"end_date": "2027-01-31"}, {"clear_end_date": True}]:
            self.search_results = [self._campaign("ENABLED", "ENDED")]
            with self.assertRaisesRegex(ToolError, "restart its spending"):
                campaigns.update_campaign_settings("1", 5, **kwargs)
        self.service.mutate.assert_not_called()
        # A paused campaign only restarts through enable_campaign.
        self.search_results = [self._campaign("PAUSED", "ENDED")]
        campaigns.update_campaign_settings("1", 5, end_date="2027-01-31")
        self.assertEqual(len(self.mutate_calls()), 1)

    def test_clear_end_date_masks_the_unset_field(self):
        self.search_results = [self._campaign()]
        result = campaigns.update_campaign_settings(
            "1", 5, clear_end_date=True, validate_only=True
        )
        [[op]] = self.mutate_calls()
        campaign = op.campaign_operation.update
        self.assertNotIn("end_date_time", campaign)
        self.assertEqual(
            list(op.campaign_operation.update_mask.paths), ["end_date_time"]
        )
        self.assertTrue(
            self.service.mutate.call_args.kwargs["request"].validate_only
        )
        self.assertEqual(result["changed"], {"end_date": None})
        self.assertTrue(result["validate_only"])

    def test_rejects_bad_name_and_end_date(self):
        for kwargs in [
            {"name": "  "},
            {"end_date": "12/31/2026"},
            {"end_date": ""},
            {"end_date": "2026-12-31", "clear_end_date": True},
        ]:
            with self.assertRaises(ToolError, msg=kwargs):
                campaigns.update_campaign_settings("1", 5, **kwargs)
        self.service.mutate.assert_not_called()


class TestUpdateAccountTracking(MutateToolTestCase):

    def test_updates_only_given_fields(self):
        core.update_account_tracking(
            "1",
            final_url_suffix="utm_source=google&utm_campaign={campaignid}",
            auto_tagging_enabled=True,
        )
        [operations] = self.mutate_calls()
        [op] = operations
        customer = (
            op.customer_operation.update
        )  # CustomerOperation is update-only
        self.assertEqual(customer.resource_name, "customers/1")
        self.assertEqual(
            customer.final_url_suffix,
            "utm_source=google&utm_campaign={campaignid}",
        )
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
        self.search_results = [
            [
                _goal("PURCHASE", "WEBSITE", False),
                _goal("DOWNLOAD", "APP", True),
                _goal("SIGNUP", "WEBSITE", False),
            ]
        ]
        result = conversions.set_campaign_conversion_goals(
            "1", 5, ["purchase:website"]
        )
        [operations] = self.mutate_calls()
        updates = {
            g.resource_name: g.biddable
            for g in _ops(
                operations, "campaign_conversion_goal_operation", "update"
            )
        }
        self.assertEqual(
            updates, {"rn/PURCHASE~WEBSITE": True, "rn/DOWNLOAD~APP": False}
        )
        self.assertEqual(result["biddable"], ["PURCHASE:WEBSITE"])

    def test_refuses_unknown_goal(self):
        self.search_results = [[_goal("PURCHASE", "WEBSITE", True)]]
        with self.assertRaises(ToolError):
            conversions.set_campaign_conversion_goals("1", 5, ["LEAD:WEBSITE"])


class TestAddCampaignAssets(MutateToolTestCase):

    def test_creates_once_links_each_campaign_and_skips_existing(self):
        self.search_results = [
            [
                {
                    "campaign.id": 6,
                    "campaign_asset.field_type": "CALLOUT",
                    "asset.callout_asset.callout_text": "No Subscription",
                    "asset.id": 9,
                },
            ]
        ]
        result = assets.add_campaign_assets(
            "1",
            [5, 6],
            sitelinks=[
                assets.Sitelink(
                    text="FAQ",
                    final_url="https://example.com/faq",
                    description1="How it works",
                    description2="Pricing",
                )
            ],
            callouts=["No Subscription"],
            structured_snippet=assets.StructuredSnippet(
                header="Styles", values=["A", "B", "C"]
            ),
            price=assets.PriceAsset(
                type="SERVICE_TIERS",
                currency_code="USD",
                items=[
                    assets.PriceItem(
                        header=f"Tier {i}",
                        description="One-time",
                        price=i * 5.0,
                        final_url="https://example.com",
                    )
                    for i in range(3)
                ],
            ),
            business_name="Example",
            business_logo_asset_id=77,
        )
        [operations] = self.mutate_calls()
        created_assets = _ops(operations, "asset_operation", "create")
        self.assertEqual(
            len(created_assets), 5
        )  # sitelink, callout, snippet, price, name
        [price] = [a for a in created_assets if a.price_asset.price_offerings]
        self.assertEqual(
            price.price_asset.price_offerings[1].price.amount_micros, 5_000_000
        )
        links = _ops(operations, "campaign_asset_operation", "create")
        pairs = {(l.campaign, l.field_type.name) for l in links}
        self.assertIn(("customers/1/campaigns/5", "CALLOUT"), pairs)
        self.assertNotIn(("customers/1/campaigns/6", "CALLOUT"), pairs)
        self.assertIn(("customers/1/campaigns/6", "BUSINESS_LOGO"), pairs)
        self.assertEqual(len(links), 11)
        self.assertIn(
            "CALLOUT: No Subscription", result["campaigns"]["6"]["skipped"]
        )
        # Newly created assets are linked through their temporary resource names.
        temp_names = {a.resource_name for a in created_assets}
        self.assertTrue(
            all(n.startswith("customers/1/assets/-") for n in temp_names)
        )

    def test_validates_lengths_and_price_items(self):
        with self.assertRaises(ToolError):
            assets.add_campaign_assets("1", [5], callouts=["x" * 26])
        with self.assertRaises(ToolError):
            assets.add_campaign_assets(
                "1",
                [5],
                structured_snippet=assets.StructuredSnippet(
                    header="Colors", values=["a", "b", "c"]
                ),
            )
        with self.assertRaises(ToolError):
            assets.add_campaign_assets(
                "1",
                [5],
                price=assets.PriceAsset(
                    type="SERVICES", currency_code="USD", items=[]
                ),
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

        self.search_results = [
            [
                sub("USE_BROAD_MATCH_KEYWORD", "ENABLED"),
                sub("KEYWORD", "PAUSED"),
                sub("UNKNOWN", "ENABLED"),
            ]
        ]
        result = campaigns.set_auto_apply_recommendations("1")
        [operations] = self.mutate_calls()
        updates = _ops(
            operations, "recommendation_subscription_operation", "update"
        )
        self.assertEqual([u.status.name for u in updates], ["PAUSED"])
        self.assertEqual(result["changed"], ["USE_BROAD_MATCH_KEYWORD"])
        self.assertEqual(result["unsupported_unknown_types"], 1)


class TestSetCpcBids(MutateToolTestCase):

    def _group(self, strategy="MANUAL_CPC"):
        return {
            "ad_group.resource_name": "customers/1/adGroups/7",
            "ad_group.name": "App intent",
            "ad_group.cpc_bid_micros": 250000,
            "campaign.bidding_strategy_type": strategy,
        }

    def test_sets_default_and_keyword_bids(self):
        self.search_results = [
            [self._group()],
            [
                {
                    "ad_group_criterion.resource_name": "customers/1/adGroupCriteria/7~9",
                    "ad_group_criterion.keyword.text": "baby name app",
                    "ad_group_criterion.keyword.match_type": "EXACT",
                    "ad_group_criterion.cpc_bid_micros": 0,
                }
            ],
        ]
        result = campaigns.set_cpc_bids(
            "1", 5, default_cpc=0.35, keyword_bids={"[Baby Name App]": 0.45}
        )
        [operations] = self.mutate_calls()
        [ag] = _ops(operations, "ad_group_operation", "update")
        self.assertEqual(ag.cpc_bid_micros, 350000)
        [kw] = _ops(operations, "ad_group_criterion_operation", "update")
        self.assertEqual(kw.resource_name, "customers/1/adGroupCriteria/7~9")
        self.assertEqual(kw.cpc_bid_micros, 450000)
        self.assertEqual(result["default_cpc"], {"before": 0.25, "after": 0.35})

    def test_refuses_bids_over_guardrail_and_smart_bidding(self):
        from ads_mcp import guardrails
        from unittest.mock import patch

        with patch.object(
            guardrails,
            "get_limits",
            return_value=guardrails.SpendLimits(max_cpc_bid=1.0),
        ):
            with self.assertRaises(ToolError):
                campaigns.set_cpc_bids("1", 5, default_cpc=1.5)
        self.search_results = [[self._group("MAXIMIZE_CONVERSIONS")]]
        with self.assertRaises(ToolError):
            campaigns.set_cpc_bids("1", 5, default_cpc=0.3)

    def test_clearing_a_keyword_bid_masks_the_unset_field(self):
        keyword = {
            "ad_group_criterion.resource_name": "customers/1/adGroupCriteria/7~9",
            "ad_group_criterion.keyword.text": "running shoes",
            "ad_group_criterion.keyword.match_type": "EXACT",
            "ad_group_criterion.cpc_bid_micros": 900_000,
        }
        self.search_results = [[self._group()], [keyword]]
        result = campaigns.set_cpc_bids(
            "1", 5, keyword_bids={"[running shoes]": 0}
        )
        [[op]] = self.mutate_calls()
        update = op.ad_group_criterion_operation
        self.assertNotIn("cpc_bid_micros", update.update)
        self.assertEqual(list(update.update_mask.paths), ["cpc_bid_micros"])
        self.assertIsNone(result["keywords"]["[running shoes]"]["after"])

    def test_clearing_refused_when_the_default_bid_is_above_the_limit(self):
        from ads_mcp import guardrails
        from unittest.mock import patch

        keyword = {
            "ad_group_criterion.resource_name": "customers/1/adGroupCriteria/7~9",
            "ad_group_criterion.keyword.text": "running shoes",
            "ad_group_criterion.keyword.match_type": "EXACT",
            "ad_group_criterion.cpc_bid_micros": 900_000,
        }
        group = {**self._group(), "ad_group.cpc_bid_micros": 3_000_000}
        with patch.object(
            guardrails,
            "get_limits",
            return_value=guardrails.SpendLimits(max_cpc_bid=2.0),
        ):
            self.search_results = [[group], [keyword]]
            with self.assertRaisesRegex(ToolError, "clearing these keyword"):
                campaigns.set_cpc_bids(
                    "1", 5, keyword_bids={"[running shoes]": 0}
                )
            self.service.mutate.assert_not_called()
            self.search_results = [[group], [keyword]]
            campaigns.set_cpc_bids(
                "1", 5, default_cpc=1.0, keyword_bids={"[running shoes]": 0}
            )
        self.assertEqual(len(self.mutate_calls()), 1)

    def test_unknown_keyword_is_an_error(self):
        self.search_results = [[self._group()], []]
        with self.assertRaises(ToolError):
            campaigns.set_cpc_bids("1", 5, keyword_bids={"[nope]": 0.3})
