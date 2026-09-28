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

"""Tests for the targeting tools."""

import unittest
from unittest.mock import patch

from fastmcp.exceptions import ToolError

import ads_mcp.guardrails as guardrails
from ads_mcp.tools import targeting
from tests.tools.ads_fakes import MutateToolTestCase


def _location(rn, geo, negative=False):
    return {
        "campaign_criterion.resource_name": rn,
        "campaign_criterion.location.geo_target_constant": geo,
        "campaign_criterion.negative": negative,
    }


class TestSetGeoTargets(MutateToolTestCase):

    def test_add_skips_existing_locations(self):
        self.search_results = [[_location("rn/us", "geoTargetConstants/2840")]]
        result = targeting.set_geo_targets(
            "1", 5, location_ids=[2840, 2124], excluded_location_ids=[21137]
        )

        [operations] = self.mutate_calls()
        created = self.created(operations, "campaign_criterion_operation")
        self.assertEqual(
            [
                (c.campaign, c.location.geo_target_constant, c.negative)
                for c in created
            ],
            [
                ("customers/1/campaigns/5", "geoTargetConstants/2124", False),
                ("customers/1/campaigns/5", "geoTargetConstants/21137", True),
            ],
        )
        self.assertEqual(result["removed"], [])
        self.assertIn("campaign_criterion.type = 'LOCATION'", self.queries[0])

    def test_replace_removes_other_locations(self):
        self.search_results = [
            [
                _location("rn/us", "geoTargetConstants/2840"),
                _location("rn/ca", "geoTargetConstants/2124"),
            ]
        ]
        result = targeting.set_geo_targets(
            "1", 5, location_ids=[2840], mode="replace"
        )

        [operations] = self.mutate_calls()
        self.assertEqual(
            [op.campaign_criterion_operation.remove for op in operations],
            ["rn/ca"],
        )
        self.assertEqual(result["removed"], ["rn/ca"])

    def test_refuses_to_remove_all_targeted_locations(self):
        self.search_results = [[_location("rn/us", "geoTargetConstants/2840")]]
        with self.assertRaisesRegex(ToolError, "all countries"):
            targeting.set_geo_targets(
                "1", 5, location_ids=[2840], mode="remove"
            )
        self.service.mutate.assert_not_called()

    def test_no_changes_sends_no_request(self):
        self.search_results = [[_location("rn/us", "geoTargetConstants/2840")]]
        result = targeting.set_geo_targets("1", 5, location_ids=[2840])
        self.service.mutate.assert_not_called()
        self.assertEqual(result["added"], [])


class TestSetNegativeKeywords(MutateToolTestCase):

    def test_matches_existing_keywords_case_insensitively(self):
        self.search_results = [
            [
                {
                    "campaign_criterion.resource_name": "rn/free",
                    "campaign_criterion.keyword.text": "Free",
                    "campaign_criterion.keyword.match_type": "BROAD",
                }
            ]
        ]
        result = targeting.set_negative_keywords(
            "1", 5, ["free", "[jobs]", '"cheap loans"']
        )

        [operations] = self.mutate_calls()
        created = self.created(operations, "campaign_criterion_operation")
        self.assertEqual(
            [(c.keyword.text, c.keyword.match_type.name) for c in created],
            [("jobs", "EXACT"), ("cheap loans", "PHRASE")],
        )
        self.assertTrue(all(c.negative for c in created))
        self.assertEqual(
            result["added"], [["jobs", "EXACT"], ["cheap loans", "PHRASE"]]
        )


class TestSetLanguageTargets(MutateToolTestCase):

    def test_adds_languages(self):
        targeting.set_language_targets("1", 5, [1000, "1003"])
        [operations] = self.mutate_calls()
        created = self.created(operations, "campaign_criterion_operation")
        self.assertEqual(
            [c.language.language_constant for c in created],
            ["languageConstants/1000", "languageConstants/1003"],
        )


class TestSetAudienceSignals(MutateToolTestCase):

    def test_replace_keeps_casing_and_removes_unlisted(self):
        self.search_results = [
            [
                {
                    "asset_group_signal.resource_name": "rn/old",
                    "asset_group_signal.audience.audience": "",
                    "asset_group_signal.search_theme.text": "old theme",
                },
                {
                    "asset_group_signal.resource_name": "rn/keep",
                    "asset_group_signal.audience.audience": (
                        "customers/1/audiences/7"
                    ),
                    "asset_group_signal.search_theme.text": "",
                },
            ]
        ]
        result = targeting.set_audience_signals(
            "1",
            8,
            audience_ids=[7],
            search_themes=["Habit Tracker App"],
            mode="replace",
        )

        [operations] = self.mutate_calls()
        created = self.created(operations, "asset_group_signal_operation")
        self.assertEqual(
            [(s.asset_group, s.search_theme.text) for s in created],
            [("customers/1/assetGroups/8", "Habit Tracker App")],
        )
        self.assertEqual(result["removed"], ["rn/old"])


class TestListAudiences(MutateToolTestCase):

    def test_lists_enabled_audiences(self):
        self.search_results = [
            [{"audience.id": 7, "audience.name": "App users"}]
        ]
        self.assertEqual(
            targeting.list_audiences("1"),
            [{"audience_id": "7", "name": "App users", "description": None}],
        )


def _ad_group(strategy="MANUAL_CPC", channel="SEARCH", default_bid=1.0):
    return {
        "ad_group.id": 7,
        "ad_group.name": "Shoes",
        "ad_group.status": "ENABLED",
        "ad_group.cpc_bid_micros": int(default_bid * 1_000_000),
        "campaign.id": 5,
        "campaign.advertising_channel_type": channel,
        "campaign.bidding_strategy_type": strategy,
    }


def _keyword(
    ad_group_id,
    criterion_id,
    text,
    match_type,
    status="ENABLED",
    bid=1.0,
    strategy="MANUAL_CPC",
):
    return {
        "ad_group_criterion.effective_cpc_bid_micros": int(bid * 1_000_000),
        "campaign.bidding_strategy_type": strategy,
        "ad_group_criterion.resource_name": (
            f"customers/1/adGroupCriteria/{ad_group_id}~{criterion_id}"
        ),
        "ad_group_criterion.criterion_id": criterion_id,
        "ad_group_criterion.keyword.text": text,
        "ad_group_criterion.keyword.match_type": match_type,
        "ad_group_criterion.status": status,
        "ad_group.id": ad_group_id,
    }


class TestAddKeywords(MutateToolTestCase):

    def setUp(self):
        super().setUp()
        self.limits = guardrails.SpendLimits()
        limits_patch = patch(
            "ads_mcp.guardrails.get_limits", side_effect=lambda _: self.limits
        )
        limits_patch.start()
        self.addCleanup(limits_patch.stop)

    def test_adds_new_keywords_and_skips_existing(self):
        self.search_results = [
            [_ad_group()],
            [_keyword(7, 11, "Running Shoes", "EXACT", "PAUSED")],
        ]
        result = targeting.add_keywords(
            "1",
            ["[running shoes]", "trail shoes", '"Trail Shoes"', "trail shoes"],
            campaign_id=5,
            keyword_bids={'"trail shoes"': 1.2},
        )

        [operations] = self.mutate_calls()
        created = self.created(operations, "ad_group_criterion_operation")
        self.assertEqual(
            [
                (
                    c.ad_group,
                    c.keyword.text,
                    c.keyword.match_type.name,
                    c.status.name,
                    c.cpc_bid_micros,
                )
                for c in created
            ],
            [
                (
                    "customers/1/adGroups/7",
                    "trail shoes",
                    "BROAD",
                    "ENABLED",
                    0,
                ),
                (
                    "customers/1/adGroups/7",
                    "trail shoes",
                    "PHRASE",
                    "ENABLED",
                    1_200_000,
                ),
            ],
        )
        self.assertEqual(
            result["added"],
            [
                {"keyword": "trail shoes", "criterion_id": "1000"},
                {
                    "keyword": '"trail shoes"',
                    "max_cpc": 1.2,
                    "criterion_id": "1001",
                },
            ],
        )
        self.assertEqual(
            result["skipped_existing"],
            [
                {
                    "keyword": "[running shoes]",
                    "criterion_id": "11",
                    "status": "PAUSED",
                }
            ],
        )
        self.assertIn("campaign.id = 5", self.queries[0])
        self.assertIn("ad_group.id = 7", self.queries[1])
        self.assertIn("ad_group_criterion.negative = FALSE", self.queries[1])

    def test_bare_keywords_use_match_type(self):
        self.search_results = [[_ad_group()], []]
        targeting.add_keywords(
            "1", ["running shoes"], ad_group_id=7, match_type="EXACT"
        )
        [operations] = self.mutate_calls()
        [created] = self.created(operations, "ad_group_criterion_operation")
        self.assertEqual(created.keyword.match_type.name, "EXACT")

    def test_nothing_new_sends_no_request(self):
        self.search_results = [
            [_ad_group()],
            [_keyword(7, 11, "running shoes", "BROAD")],
        ]
        result = targeting.add_keywords("1", ["running shoes"], ad_group_id=7)
        self.service.mutate.assert_not_called()
        self.assertEqual(result["added"], [])

    def test_keyword_bids_are_capped_by_the_guardrail(self):
        self.limits = guardrails.SpendLimits(max_cpc_bid=1.0)
        with self.assertRaisesRegex(ToolError, "max_cpc_bid of 1.0"):
            targeting.add_keywords(
                "1", ["[shoes]"], ad_group_id=7, keyword_bids={"[shoes]": 1.5}
            )
        with self.assertRaisesRegex(ToolError, "not in keywords"):
            targeting.add_keywords(
                "1", ["[shoes]"], ad_group_id=7, keyword_bids={"shoes": 0.5}
            )
        self.assertEqual(self.queries, [])

    def test_new_keywords_cannot_inherit_a_default_bid_over_the_limit(self):
        self.limits = guardrails.SpendLimits(max_cpc_bid=2.0)
        self.search_results = [
            [_ad_group(default_bid=5.0)],
            [_keyword(7, 11, "running shoes", "EXACT")],
        ]
        with self.assertRaisesRegex(ToolError, r"\(trail shoes: 5.0\)"):
            targeting.add_keywords(
                "1", ["[running shoes]", "trail shoes"], ad_group_id=7
            )
        self.service.mutate.assert_not_called()
        # Keyword bids within the limit, or Smart Bidding, are fine.
        self.search_results = [[_ad_group(default_bid=5.0)], []]
        targeting.add_keywords(
            "1", ["trail shoes"], ad_group_id=7, keyword_bids={"trail shoes": 1}
        )
        self.search_results = [
            [_ad_group("MAXIMIZE_CONVERSIONS", default_bid=5.0)],
            [],
        ]
        targeting.add_keywords("1", ["trail shoes"], ad_group_id=7)
        self.assertEqual(len(self.mutate_calls()), 2)

    def test_refuses_bids_under_smart_bidding_and_non_search(self):
        self.search_results = [[_ad_group("MAXIMIZE_CONVERSIONS")]]
        with self.assertRaisesRegex(ToolError, "MANUAL_CPC"):
            targeting.add_keywords(
                "1", ["[shoes]"], ad_group_id=7, keyword_bids={"[shoes]": 0.5}
            )
        self.search_results = [[_ad_group(channel="DISPLAY")]]
        with self.assertRaisesRegex(ToolError, "Search campaigns"):
            targeting.add_keywords("1", ["[shoes]"], ad_group_id=7)
        with self.assertRaisesRegex(ToolError, "at least one keyword"):
            targeting.add_keywords("1", [" "], ad_group_id=7)
        self.service.mutate.assert_not_called()

    def test_validate_only(self):
        self.search_results = [[_ad_group()], []]
        result = targeting.add_keywords(
            "1", ["[shoes]"], ad_group_id=7, validate_only=True
        )
        self.assertTrue(
            self.service.mutate.call_args.kwargs["request"].validate_only
        )
        self.assertEqual(result["added"], [{"keyword": "[shoes]"}])
        self.assertTrue(result["validate_only"])


class TestSetKeywordStatus(MutateToolTestCase):

    def test_pauses_by_text_and_criterion_id_in_a_campaign(self):
        self.search_results = [
            [
                _keyword(7, 11, "running shoes", "EXACT"),
                _keyword(8, 11, "running shoes", "EXACT"),
                _keyword(8, 12, "trail shoes", "BROAD", "PAUSED"),
                _keyword(8, 13, "running shoes", "PHRASE"),
            ]
        ]
        result = targeting.set_keyword_status(
            "1",
            "PAUSED",
            keywords=["[Running Shoes]"],
            criterion_ids=[12],
            campaign_id=5,
        )

        [operations] = self.mutate_calls()
        self.assertEqual(
            [
                op.ad_group_criterion_operation._pb.WhichOneof("operation")
                for op in operations
            ],
            ["update", "update"],
        )
        self.assertEqual(
            [
                (
                    op.ad_group_criterion_operation.update.resource_name,
                    op.ad_group_criterion_operation.update.status.name,
                    list(op.ad_group_criterion_operation.update_mask.paths),
                )
                for op in operations
            ],
            [
                ("customers/1/adGroupCriteria/7~11", "PAUSED", ["status"]),
                ("customers/1/adGroupCriteria/8~11", "PAUSED", ["status"]),
            ],
        )
        self.assertEqual(
            [c["ad_group_id"] for c in result["changed"]], ["7", "8"]
        )
        self.assertEqual(
            result["unchanged"],
            [
                {
                    "keyword": "trail shoes",
                    "criterion_id": "12",
                    "ad_group_id": "8",
                    "previous_status": "PAUSED",
                }
            ],
        )
        self.assertIn("campaign.id = 5", self.queries[0])
        self.assertIn("ad_group_criterion.status != 'REMOVED'", self.queries[0])

    def test_composite_criterion_ids_need_no_scope(self):
        self.search_results = [
            [_keyword(8, 12, "trail shoes", "BROAD", "PAUSED")]
        ]
        result = targeting.set_keyword_status(
            "1", "ENABLED", criterion_ids=["customers/1/adGroupCriteria/8~12"]
        )
        self.assertIn(
            "ad_group_criterion.resource_name IN "
            "('customers/1/adGroupCriteria/8~12')",
            self.queries[0],
        )
        [[op]] = self.mutate_calls()
        self.assertEqual(
            op.ad_group_criterion_operation.update.status.name, "ENABLED"
        )
        self.assertEqual(result["changed"][0]["previous_status"], "PAUSED")

    def test_missing_keyword_changes_nothing(self):
        self.search_results = [[_keyword(7, 11, "running shoes", "EXACT")]]
        with self.assertRaisesRegex(ToolError, r"\['running shoes'\]"):
            targeting.set_keyword_status(
                "1",
                "PAUSED",
                keywords=["[running shoes]", "running shoes"],
                ad_group_id=7,
            )
        self.service.mutate.assert_not_called()

    def test_rejects_ambiguous_or_invalid_ids(self):
        for kwargs in [
            {"keywords": ["shoes"]},
            {"criterion_ids": [12]},
            {"criterion_ids": ["12 OR 1=1"], "ad_group_id": 7},
            {"criterion_ids": ["customers/2/adGroupCriteria/8~12"]},
            {"criterion_ids": ["8~12~3"]},
            {"ad_group_id": 7},
        ]:
            with self.assertRaises(ToolError, msg=kwargs):
                targeting.set_keyword_status("1", "PAUSED", **kwargs)
        with self.assertRaises(ToolError):
            targeting.set_keyword_status(
                "1", "REMOVED", keywords=["shoes"], ad_group_id=7
            )
        self.assertEqual(self.queries, [])

    def test_enabling_checks_effective_bids_in_manual_cpc(self):
        limits = patch(
            "ads_mcp.guardrails.get_limits",
            return_value=guardrails.SpendLimits(max_cpc_bid=2.0),
        )
        with limits as get_limits:
            self.search_results = [
                [
                    _keyword(7, 11, "shoes", "BROAD", "PAUSED", bid=3.0),
                    _keyword(7, 12, "boots", "BROAD", "PAUSED", bid=1.0),
                ]
            ]
            with self.assertRaisesRegex(
                ToolError, r"\(shoes in ad group 7: 3.0\)"
            ):
                targeting.set_keyword_status(
                    "1", "ENABLED", keywords=["shoes", "boots"], ad_group_id=7
                )
            self.service.mutate.assert_not_called()

            # Smart Bidding sets its own bids.
            self.search_results = [
                [
                    _keyword(
                        7, 11, "shoes", "BROAD", "PAUSED", 3.0, "TARGET_SPEND"
                    )
                ]
            ]
            targeting.set_keyword_status(
                "1", "ENABLED", keywords=["shoes"], ad_group_id=7
            )
            # Pausing never reads the limits.
            get_limits.reset_mock()
            self.search_results = [
                [_keyword(7, 11, "shoes", "BROAD", "ENABLED", bid=3.0)]
            ]
            targeting.set_keyword_status(
                "1", "PAUSED", keywords=["shoes"], ad_group_id=7
            )
            get_limits.assert_not_called()
        self.assertEqual(len(self.mutate_calls()), 2)

    def test_validate_only(self):
        self.search_results = [[_keyword(7, 11, "shoes", "BROAD")]]
        result = targeting.set_keyword_status(
            "1", "PAUSED", keywords=["shoes"], ad_group_id=7, validate_only=True
        )
        self.assertTrue(
            self.service.mutate.call_args.kwargs["request"].validate_only
        )
        self.assertTrue(result["validate_only"])


if __name__ == "__main__":
    unittest.main()
