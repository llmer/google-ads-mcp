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

from fastmcp.exceptions import ToolError

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


if __name__ == "__main__":
    unittest.main()
