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

"""Tests for the campaign tools."""

import unittest

from fastmcp.exceptions import ToolError

from unittest.mock import patch

import ads_mcp.guardrails as guardrails
from ads_mcp.tools import campaigns
from tests.tools.ads_fakes import MutateToolTestCase

_HEADLINES = ["Track spending", "Budget in minutes", "Free budget app"]
_LONG_HEADLINES = ["The budgeting app that does the math for you"]
_DESCRIPTIONS = [
    "Plan every dollar.",
    "Sync your accounts and see where money goes.",
]


class TestCreateSearchCampaign(MutateToolTestCase):

    def _create(self, **kwargs):
        args = dict(
            customer_id="123-456-7890",
            name="Web signups",
            daily_budget=20,
            final_url="https://example.com",
            headlines=_HEADLINES,
            descriptions=_DESCRIPTIONS,
            keywords=["budget app", "[expense tracker]"],
            negative_keywords=["free"],
            geo_target_ids=[2840],
            language_ids=["1000"],
        )
        args.update(kwargs)
        return campaigns.create_search_campaign(**args)

    def test_creates_all_entities_in_one_atomic_request(self):
        result = self._create(target_cpa=4.5)

        [operations] = self.mutate_calls()
        [budget] = self.created(operations, "campaign_budget_operation")
        [campaign] = self.created(operations, "campaign_operation")
        criteria = self.created(operations, "campaign_criterion_operation")
        [ad_group] = self.created(operations, "ad_group_operation")
        keywords = self.created(operations, "ad_group_criterion_operation")
        [ad] = self.created(operations, "ad_group_ad_operation")

        self.assertEqual(budget.amount_micros, 20_000_000)
        self.assertEqual(campaign.status.name, "PAUSED")
        self.assertEqual(campaign.advertising_channel_type.name, "SEARCH")
        self.assertEqual(campaign.campaign_budget, budget.resource_name)
        self.assertEqual(
            campaign.maximize_conversions.target_cpa_micros, 4_500_000
        )
        self.assertEqual(
            campaign.contains_eu_political_advertising.name,
            "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
        )
        self.assertFalse(campaign.network_settings.target_content_network)
        self.assertEqual(
            [
                (
                    c.location.geo_target_constant,
                    c.language.language_constant,
                    c.keyword.text,
                    c.negative,
                )
                for c in criteria
            ],
            [
                ("geoTargetConstants/2840", "", "", False),
                ("", "languageConstants/1000", "", False),
                ("", "", "free", True),
            ],
        )
        self.assertEqual(ad_group.campaign, campaign.resource_name)
        self.assertEqual(
            [(k.keyword.text, k.keyword.match_type.name) for k in keywords],
            [("budget app", "PHRASE"), ("expense tracker", "EXACT")],
        )
        self.assertEqual(
            [h.text for h in ad.ad.responsive_search_ad.headlines], _HEADLINES
        )
        self.assertEqual(ad.ad_group, ad_group.resource_name)
        self.assertEqual(result["status"], "PAUSED")
        self.assertEqual(result["campaign_id"], "1001")
        self.assertNotIn("warnings", result)

    def test_customer_id_is_cleaned(self):
        self._create()
        self.assertEqual(
            self.service.mutate.call_args.kwargs["request"].customer_id,
            "1234567890",
        )

    def test_validate_only(self):
        result = self._create(validate_only=True)
        self.assertTrue(
            self.service.mutate.call_args.kwargs["request"].validate_only
        )
        self.assertTrue(result["validate_only"])

    def test_manual_cpc_requires_max_cpc(self):
        with self.assertRaisesRegex(ToolError, "max_cpc"):
            self._create(bidding_strategy="MANUAL_CPC")

    def test_manual_cpc_sets_ad_group_bid(self):
        self._create(bidding_strategy="MANUAL_CPC", max_cpc=1.25)
        [operations] = self.mutate_calls()
        [campaign] = self.created(operations, "campaign_operation")
        [ad_group] = self.created(operations, "ad_group_operation")
        self.assertEqual(
            campaign._pb.WhichOneof("campaign_bidding_strategy"), "manual_cpc"
        )
        self.assertEqual(ad_group.cpc_bid_micros, 1_250_000)

    def test_warns_without_geo_targets(self):
        result = self._create(geo_target_ids=None)
        self.assertIn("all countries", result["warnings"][0])

    def test_rejects_too_few_headlines(self):
        with self.assertRaisesRegex(ToolError, "headlines"):
            self._create(headlines=["One"])
        self.service.mutate.assert_not_called()


class TestCreatePmaxCampaign(MutateToolTestCase):

    def _create(self, **kwargs):
        args = dict(
            customer_id="1",
            name="PMax",
            daily_budget=50,
            final_url="https://example.com",
            headlines=_HEADLINES,
            long_headlines=_LONG_HEADLINES,
            descriptions=_DESCRIPTIONS,
            business_name="Budgetly",
            marketing_image_asset_ids=["11"],
            square_marketing_image_asset_ids=[12],
            logo_asset_ids=["customers/1/assets/13"],
            geo_target_ids=[2840],
            audience_ids=[77],
            search_themes=["budget planner"],
        )
        args.update(kwargs)
        return campaigns.create_pmax_campaign(**args)

    def test_creates_text_assets_first_then_campaign_atomically(self):
        result = self._create(
            bidding_strategy="MAXIMIZE_CONVERSION_VALUE", target_roas=3.5
        )

        text_assets_call, campaign_call = self.mutate_calls()
        texts = [
            a.text_asset.text
            for a in self.created(text_assets_call, "asset_operation")
        ]
        self.assertEqual(texts, _HEADLINES + _DESCRIPTIONS)

        [campaign] = self.created(campaign_call, "campaign_operation")
        [asset_group] = self.created(campaign_call, "asset_group_operation")
        links = self.created(campaign_call, "asset_group_asset_operation")
        signals = self.created(campaign_call, "asset_group_signal_operation")

        self.assertEqual(
            campaign.advertising_channel_type.name, "PERFORMANCE_MAX"
        )
        self.assertEqual(campaign.status.name, "PAUSED")
        self.assertEqual(campaign.maximize_conversion_value.target_roas, 3.5)
        self.assertEqual(asset_group.campaign, campaign.resource_name)
        self.assertEqual(list(asset_group.final_urls), ["https://example.com"])

        by_field = {}
        for link in links:
            self.assertEqual(link.asset_group, asset_group.resource_name)
            by_field.setdefault(link.field_type.name, []).append(link.asset)
        self.assertEqual(len(by_field["HEADLINE"]), 3)
        self.assertEqual(len(by_field["DESCRIPTION"]), 2)
        # Headlines link to the assets created by the first request.
        self.assertEqual(by_field["HEADLINE"][0], "customers/1/assets/1000")
        self.assertEqual(by_field["MARKETING_IMAGE"], ["customers/1/assets/11"])
        self.assertEqual(by_field["LOGO"], ["customers/1/assets/13"])
        self.assertIn("BUSINESS_NAME", by_field)
        self.assertIn("LONG_HEADLINE", by_field)
        self.assertEqual(
            [(s.audience.audience, s.search_theme.text) for s in signals],
            [("customers/1/audiences/77", ""), ("", "budget planner")],
        )
        self.assertEqual(result["status"], "PAUSED")

    def test_validate_only_creates_text_assets_inline(self):
        self._create(validate_only=True)

        [operations] = self.mutate_calls()
        inline_texts = [
            a.text_asset.text
            for a in self.created(operations, "asset_operation")
        ]
        for text in _HEADLINES + _DESCRIPTIONS:
            self.assertIn(text, inline_texts)
        self.assertTrue(
            self.service.mutate.call_args.kwargs["request"].validate_only
        )

    def test_brand_guidelines_link_brand_assets_to_campaign(self):
        self._create(brand_guidelines_enabled=True)

        _, operations = self.mutate_calls()
        campaign_links = self.created(operations, "campaign_asset_operation")
        self.assertEqual(
            sorted(link.field_type.name for link in campaign_links),
            ["BUSINESS_NAME", "LOGO"],
        )

    def test_requires_square_image(self):
        with self.assertRaisesRegex(ToolError, "square"):
            self._create(square_marketing_image_asset_ids=[])


class TestCreateAppCampaign(MutateToolTestCase):

    def _create(self, **kwargs):
        args = dict(
            customer_id="1",
            name="App installs",
            daily_budget=30,
            app_id="com.example.budget",
            app_store="GOOGLE_APP_STORE",
            headlines=["Budget smarter"],
            descriptions=["Know where your money goes."],
            image_asset_ids=[5],
            video_asset_ids=[6],
            geo_target_ids=[2840],
        )
        args.update(kwargs)
        return campaigns.create_app_campaign(**args)

    def _campaign(self):
        [operations] = self.mutate_calls()
        [campaign] = self.created(operations, "campaign_operation")
        return campaign, operations

    def test_installs_with_target_cost(self):
        self._create(target_cpa=2)
        campaign, operations = self._campaign()
        self.assertEqual(
            campaign.advertising_channel_type.name, "MULTI_CHANNEL"
        )
        self.assertEqual(
            campaign.advertising_channel_sub_type.name, "APP_CAMPAIGN"
        )
        self.assertEqual(
            campaign.app_campaign_setting.bidding_strategy_goal_type.name,
            "OPTIMIZE_INSTALLS_TARGET_INSTALL_COST",
        )
        self.assertEqual(campaign.target_cpa.target_cpa_micros, 2_000_000)
        [ad] = self.created(operations, "ad_group_ad_operation")
        self.assertEqual(
            [i.asset for i in ad.ad.app_ad.images], ["customers/1/assets/5"]
        )
        self.assertEqual(
            [v.asset for v in ad.ad.app_ad.youtube_videos],
            ["customers/1/assets/6"],
        )

    def test_installs_without_target_maximizes_conversions(self):
        self._create()
        campaign, _ = self._campaign()
        self.assertEqual(
            campaign.app_campaign_setting.bidding_strategy_goal_type.name,
            "OPTIMIZE_INSTALLS_WITHOUT_TARGET_INSTALL_COST",
        )
        self.assertEqual(
            campaign._pb.WhichOneof("campaign_bidding_strategy"),
            "maximize_conversions",
        )

    def test_in_app_value_with_roas(self):
        self._create(
            goal="IN_APP_VALUE", target_roas=2.0, conversion_action_ids=[99]
        )
        campaign, _ = self._campaign()
        self.assertEqual(
            campaign.app_campaign_setting.bidding_strategy_goal_type.name,
            "OPTIMIZE_RETURN_ON_ADVERTISING_SPEND",
        )
        self.assertEqual(campaign.target_roas.target_roas, 2.0)
        self.assertEqual(
            list(campaign.selective_optimization.conversion_actions),
            ["customers/1/conversionActions/99"],
        )

    def test_in_app_goals_require_conversion_actions(self):
        with self.assertRaisesRegex(ToolError, "conversion_action_ids"):
            self._create(goal="IN_APP_ACTIONS")


class TestCampaignUpdates(MutateToolTestCase):

    def test_update_budget(self):
        self.search_results = [
            [
                {
                    "campaign.campaign_budget": "customers/1/campaignBudgets/9",
                    "campaign_budget.amount_micros": 10_000_000,
                    "campaign_budget.reference_count": 1,
                    "customer.currency_code": "USD",
                }
            ]
        ]
        result = campaigns.update_budget("1", "5", 35.0)

        [[operation]] = self.mutate_calls()
        budget_op = operation.campaign_budget_operation
        self.assertEqual(
            budget_op.update.resource_name, "customers/1/campaignBudgets/9"
        )
        self.assertEqual(budget_op.update.amount_micros, 35_000_000)
        self.assertEqual(list(budget_op.update_mask.paths), ["amount_micros"])
        self.assertEqual(result["previous_daily_budget"], 10.0)
        self.assertEqual(result["new_daily_budget"], 35.0)

    def test_update_budget_refuses_shared_budget(self):
        self.search_results = [
            [
                {
                    "campaign.campaign_budget": "customers/1/campaignBudgets/9",
                    "campaign_budget.amount_micros": 10_000_000,
                    "campaign_budget.reference_count": 3,
                }
            ]
        ]
        with self.assertRaisesRegex(ToolError, "shared by 3 campaigns"):
            campaigns.update_budget("1", "5", 35.0)
        self.service.mutate.assert_not_called()

    def test_pause_and_enable(self):
        for tool, status in [
            (campaigns.pause_campaign, "PAUSED"),
            (campaigns.enable_campaign, "ENABLED"),
        ]:
            self.search_results = [
                [{"campaign.name": "C", "campaign.status": "UNKNOWN"}]
            ]
            result = tool("1", "customers/1/campaigns/5")
            operation = self.mutate_calls()[-1][0].campaign_operation
            self.assertEqual(
                operation.update.resource_name, "customers/1/campaigns/5"
            )
            self.assertEqual(operation.update.status.name, status)
            self.assertEqual(list(operation.update_mask.paths), ["status"])
            self.assertEqual(result["status"], status)

    def test_campaign_not_found(self):
        with self.assertRaisesRegex(ToolError, "not found"):
            campaigns.pause_campaign("1", "5")


class TestGetCampaign(MutateToolTestCase):

    def test_returns_config_targeting_and_metrics(self):
        self.search_results = [
            [
                {
                    "campaign.id": 5,
                    "campaign.name": "PMax",
                    "campaign.advertising_channel_type": "PERFORMANCE_MAX",
                    "campaign.target_cpa.target_cpa_micros": 0,
                    "campaign.maximize_conversions.target_cpa_micros": 3_000_000,
                    "campaign_budget.amount_micros": 50_000_000,
                    "customer.currency_code": "USD",
                }
            ],
            [{"campaign_criterion.type": "LOCATION"}],
            [{"asset_group.id": 8}],
            [{"metrics.clicks": 10, "metrics.cost_micros": 12_340_000}],
        ]
        result = campaigns.get_campaign("1", 5)

        self.assertEqual(result["daily_budget"], 50.0)
        self.assertEqual(result["target_cpa"], 3.0)
        self.assertEqual(result["asset_groups"], [{"asset_group.id": 8}])
        self.assertEqual(result["last_30_days"], {"clicks": 10, "cost": 12.34})
        self.assertIn("campaign.id = 5", self.queries[0])


class TestSpendGuardrails(MutateToolTestCase):

    def setUp(self):
        super().setUp()
        self.limits = guardrails.SpendLimits()
        limits_patch = patch(
            "ads_mcp.guardrails.get_limits", side_effect=lambda _: self.limits
        )
        limits_patch.start()
        self.addCleanup(limits_patch.stop)

    def _budget_row(self, amount, rn="customers/1/campaignBudgets/9"):
        return {
            "campaign.name": "C",
            "campaign.status": "PAUSED",
            "campaign.campaign_budget": rn,
            "campaign_budget.amount_micros": int(amount * 1_000_000),
            "campaign_budget.reference_count": 1,
        }

    def _enabled(self, *budgets):
        return [
            {
                "campaign.campaign_budget": f"customers/1/campaignBudgets/{i}",
                "campaign_budget.amount_micros": int(b * 1_000_000),
            }
            for i, b in enumerate(budgets)
        ]

    def test_create_refuses_budget_above_max(self):
        self.limits = guardrails.SpendLimits(max_daily_budget=50)
        with self.assertRaisesRegex(ToolError, "max_daily_budget"):
            campaigns.create_search_campaign(
                "1",
                "C",
                75,
                "https://example.com",
                _HEADLINES,
                _DESCRIPTIONS,
                ["kw"],
            )
        self.service.mutate.assert_not_called()

    def _create_search(self, **kwargs):
        return campaigns.create_search_campaign(
            "1",
            "C",
            10,
            "https://example.com",
            _HEADLINES,
            _DESCRIPTIONS,
            ["kw"],
            **kwargs,
        )

    def test_create_refuses_cpc_above_max_cpc_bid(self):
        self.limits = guardrails.SpendLimits(max_cpc_bid=2.0)
        with self.assertRaisesRegex(ToolError, "max_cpc_bid of 2.0"):
            self._create_search(bidding_strategy="MANUAL_CPC", max_cpc=2.5)
        with self.assertRaisesRegex(ToolError, "max_cpc_bid of 2.0"):
            self._create_search(bidding_strategy="MAXIMIZE_CLICKS", max_cpc=3)
        self.service.mutate.assert_not_called()

        self._create_search(bidding_strategy="MANUAL_CPC", max_cpc=2.0)
        [operations] = self.mutate_calls()
        [ad_group] = self.created(operations, "ad_group_operation")
        self.assertEqual(ad_group.cpc_bid_micros, 2_000_000)

    def test_create_maximize_clicks_needs_ceiling_under_max_cpc_bid(self):
        self.limits = guardrails.SpendLimits(max_cpc_bid=2.0)
        with self.assertRaisesRegex(ToolError, "needs a CPC bid ceiling"):
            self._create_search(bidding_strategy="MAXIMIZE_CLICKS")
        # Other strategies have no per-click limit to check.
        self._create_search(bidding_strategy="MAXIMIZE_CONVERSIONS")
        self.limits = guardrails.SpendLimits()
        self._create_search(bidding_strategy="MAXIMIZE_CLICKS")
        self.assertEqual(len(self.mutate_calls()), 2)

    def test_update_budget_refuses_large_increase(self):
        self.limits = guardrails.SpendLimits(max_budget_increase_percent=50)
        self.search_results = [[self._budget_row(10)]]
        with self.assertRaisesRegex(ToolError, "100% increase"):
            campaigns.update_budget("1", 5, 20)
        self.service.mutate.assert_not_called()

    def test_update_budget_checks_total_of_enabled_campaigns(self):
        self.limits = guardrails.SpendLimits(max_total_daily_budget=100)
        # Budget 9 is enabled at 10; others total 80.
        enabled = self._enabled(50, 30) + [
            {
                "campaign.campaign_budget": "customers/1/campaignBudgets/9",
                "campaign_budget.amount_micros": 10_000_000,
            }
        ]
        self.search_results = [[self._budget_row(10)], enabled]
        result = campaigns.update_budget("1", 5, 20)
        self.assertEqual(result["projected_daily_budget_total"], 100.0)

        self.search_results = [[self._budget_row(10)], enabled]
        with self.assertRaisesRegex(ToolError, "max_total_daily_budget"):
            campaigns.update_budget("1", 5, 21)

    def test_enable_refuses_when_total_would_exceed_limit(self):
        self.limits = guardrails.SpendLimits(max_total_daily_budget=100)
        self.search_results = [[self._budget_row(30)], self._enabled(50, 30)]
        with self.assertRaisesRegex(ToolError, "from 80.00 to 110.00"):
            campaigns.enable_campaign("1", 5)
        self.service.mutate.assert_not_called()

    def test_pause_is_never_blocked(self):
        self.limits = guardrails.SpendLimits(
            max_daily_budget=1, max_total_daily_budget=1
        )
        self.search_results = [[self._budget_row(30)]]
        result = campaigns.pause_campaign("1", 5)
        self.assertEqual(result["status"], "PAUSED")


class TestReadTools(MutateToolTestCase):

    def test_list_campaigns_includes_campaigns_without_traffic(self):
        self.search_results = [
            [
                {
                    "campaign.id": 1,
                    "campaign.name": "A",
                    "campaign_budget.amount_micros": 20_000_000,
                },
                {
                    "campaign.id": 2,
                    "campaign.name": "B",
                    "campaign_budget.amount_micros": 5_000_000,
                },
            ],
            [{"campaign.id": 1, "metrics.cost_micros": 7_500_000}],
        ]
        result = campaigns.list_campaigns("1", date_range="LAST_7_DAYS")

        self.assertEqual([c["id"] for c in result], [1, 2])
        self.assertEqual(result[0]["daily_budget"], 20.0)
        self.assertEqual(result[0]["last_7_days"]["cost"], 7.5)
        self.assertEqual(result[1]["last_7_days"]["cost"], 0.0)
        self.assertIn("DURING LAST_7_DAYS", self.queries[1])
        self.assertIn("('ENABLED', 'PAUSED')", self.queries[0])

    @patch(
        "ads_mcp.guardrails.get_limits",
        return_value=guardrails.SpendLimits(max_total_daily_budget=100),
    )
    def test_get_spend_overview(self, _):
        self.search_results = [
            [
                {
                    "campaign.campaign_budget": "b/1",
                    "campaign_budget.amount_micros": 30_000_000,
                },
                # A shared budget appears once per campaign.
                {
                    "campaign.campaign_budget": "b/1",
                    "campaign_budget.amount_micros": 30_000_000,
                },
                {
                    "campaign.campaign_budget": "b/2",
                    "campaign_budget.amount_micros": 15_000_000,
                },
            ],
            [{"customer.currency_code": "USD"}],
            [{"metrics.cost_micros": 12_000_000}],
            [{"metrics.cost_micros": 250_000_000}],
            [],
        ]
        result = campaigns.get_spend_overview("1")

        self.assertEqual(result["enabled_daily_budget_total"], 45.0)
        self.assertEqual(result["max_possible_spend_today"], 90.0)
        self.assertEqual(result["cost_today"], 12.0)
        self.assertEqual(result["cost_this_month"], 250.0)
        self.assertEqual(result["account_budgets"], [])
        self.assertEqual(result["guardrails"]["daily_budget_headroom"], 55.0)

    def test_get_campaign_adds_location_names(self):
        self.search_results = [
            [
                {
                    "campaign.advertising_channel_type": "SEARCH",
                    "campaign_budget.amount_micros": 1_000_000,
                }
            ],
            [
                {
                    "campaign_criterion.type": "LOCATION",
                    "campaign_criterion.location.geo_target_constant": (
                        "geoTargetConstants/2840"
                    ),
                }
            ],
            [
                {
                    "geo_target_constant.resource_name": (
                        "geoTargetConstants/2840"
                    ),
                    "geo_target_constant.canonical_name": "United States",
                }
            ],
            [],
            [],
        ]
        result = campaigns.get_campaign("1", 5)
        self.assertEqual(
            result["targeting"][0]["location_name"], "United States"
        )


if __name__ == "__main__":
    unittest.main()
