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

"""Tests for the spend guardrails."""

import unittest
from unittest.mock import patch

from fastmcp.exceptions import ToolError

import ads_mcp.guardrails as guardrails
from ads_mcp.config import ToolsConfig


class TestGetLimits(unittest.TestCase):

    @patch("ads_mcp.config.ToolsConfig.load")
    def test_account_overrides_apply_to_matching_account(self, mock_load):
        mock_load.return_value = ToolsConfig(
            {
                "guardrails": {
                    "max_daily_budget": 100,
                    "max_total_daily_budget": 500,
                    "accounts": {
                        # Unquoted YAML keys are ints; hyphens are allowed.
                        1234567890: {"max_total_daily_budget": 2000},
                        "111-222-3333": {"max_daily_budget": 10},
                    },
                }
            }
        )

        self.assertEqual(
            guardrails.get_limits("1234567890"),
            guardrails.SpendLimits(100, 2000, None),
        )
        self.assertEqual(
            guardrails.get_limits("1112223333"),
            guardrails.SpendLimits(10, 500, None),
        )
        self.assertEqual(
            guardrails.get_limits("999"),
            guardrails.SpendLimits(100, 500, None),
        )

    @patch("ads_mcp.config.ToolsConfig.load")
    def test_no_guardrails_section_means_no_limits(self, mock_load):
        mock_load.return_value = ToolsConfig({"namespaces": {"search": True}})
        self.assertEqual(guardrails.get_limits("1"), guardrails.SpendLimits())

    def test_bundled_config_parses(self):
        # The bundled config ships the section with every limit unset.
        self.assertEqual(
            ToolsConfig.load().guardrails.get("max_daily_budget"), None
        )


class TestChecks(unittest.TestCase):

    def test_check_daily_budget(self):
        limits = guardrails.SpendLimits(max_daily_budget=100)
        guardrails.check_daily_budget(limits, 100)
        with self.assertRaisesRegex(ToolError, "max_daily_budget of 100"):
            guardrails.check_daily_budget(limits, 100.01)
        guardrails.check_daily_budget(guardrails.SpendLimits(), 10**6)

    def test_check_budget_increase(self):
        limits = guardrails.SpendLimits(max_budget_increase_percent=50)
        guardrails.check_budget_increase(limits, 100, 150)
        guardrails.check_budget_increase(limits, 100, 20)
        with self.assertRaisesRegex(ToolError, "60% increase"):
            guardrails.check_budget_increase(limits, 100, 160)

    def test_check_total_daily_budget(self):
        limits = guardrails.SpendLimits(max_total_daily_budget=100)
        budgets = {"b/1": 40.0, "b/2": 30.0}

        # Changing an enabled budget replaces its amount.
        self.assertEqual(
            guardrails.check_total_daily_budget(limits, budgets, "b/1", 70),
            {
                "enabled_daily_budget_total": 70.0,
                "projected_daily_budget_total": 100.0,
            },
        )
        # Enabling a campaign with a new budget adds it.
        with self.assertRaisesRegex(ToolError, "from 70.00 to 101.00"):
            guardrails.check_total_daily_budget(limits, budgets, "b/3", 31)
        # Reductions are allowed even when already over the limit.
        guardrails.check_total_daily_budget(limits, {"b/1": 150.0}, "b/1", 120)

    def test_check_effective_cpc_bids(self):
        limits = guardrails.SpendLimits(max_cpc_bid=2.0)
        guardrails.check_effective_cpc_bids(
            limits, [("a", 2.0), ("b", None), ("c", 0)], "enabling"
        )
        # Labels may repeat; every bid is checked.
        with self.assertRaisesRegex(
            ToolError,
            r"enabling would make CPC bids above the configured max_cpc_bid "
            r"of 2.0 apply \(b: 2.5\). Lower them.",
        ):
            guardrails.check_effective_cpc_bids(
                limits, [("b", 2.5), ("b", 1.0)], "enabling", "Lower them."
            )
        guardrails.check_effective_cpc_bids(
            guardrails.SpendLimits(), [("a", 100.0)], "enabling"
        )

    @patch("ads_mcp.mutations.search")
    def test_check_serving_cpc_bids(self, search):
        search.return_value = [
            {
                "ad_group.id": 7,
                "ad_group_criterion.criterion_id": 9,
                "ad_group_criterion.type": "WEBPAGE",
                "ad_group_criterion.effective_cpc_bid_micros": 2_500_000,
            }
        ]
        with self.assertRaisesRegex(
            ToolError, r"\(webpage 9 in ad group 7: 2.5\)"
        ):
            guardrails.check_serving_cpc_bids(
                "client",
                "1",
                guardrails.SpendLimits(max_cpc_bid=2.0),
                ["ad_group.id = 7"],
                "enabling",
            )
        query = search.call_args.args[2]
        self.assertIn("campaign.bidding_strategy_type = 'MANUAL_CPC'", query)
        self.assertIn("ad_group_criterion.status = 'ENABLED'", query)
        self.assertTrue(query.endswith(" AND ad_group.id = 7"))

        # Without the guardrail nothing is read.
        search.reset_mock()
        guardrails.check_serving_cpc_bids(
            "client", "1", guardrails.SpendLimits(), ["x"], "enabling"
        )
        search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
