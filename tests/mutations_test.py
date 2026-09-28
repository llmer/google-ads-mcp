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

"""Tests for the shared mutate helpers."""

import unittest
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.ads.googleads.v25.errors.types.field_error import FieldErrorEnum
from google.ads.googleads.v25.errors.types.policy_finding_error import (
    PolicyFindingErrorEnum,
)
from google.ads.googleads.v25.errors.types.policy_violation_error import (
    PolicyViolationErrorEnum,
)

import ads_mcp.mutations as mutations
from tests.tools.ads_fakes import MutateToolTestCase, make_client


class TestMutationHelpers(unittest.TestCase):

    def test_to_micros_rounds_to_cents(self):
        self.assertEqual(mutations.to_micros(25.5), 25_500_000)
        self.assertEqual(mutations.to_micros(0.123), 120_000)
        self.assertEqual(mutations.to_micros(10), 10_000_000)
        self.assertIsNone(mutations.to_micros(None))

    def test_parse_id_accepts_ids_and_resource_names(self):
        self.assertEqual(mutations.parse_id(123), "123")
        self.assertEqual(mutations.parse_id("customers/1/campaigns/456"), "456")
        with self.assertRaises(ToolError):
            mutations.parse_id("1 OR 1=1")

    def test_parse_keyword_syntax(self):
        self.assertEqual(
            mutations.parse_keyword("[Budget App]", "BROAD"),
            ("budget app", "EXACT"),
        )
        self.assertEqual(
            mutations.parse_keyword('"budget app"', "BROAD"),
            ("budget app", "PHRASE"),
        )
        self.assertEqual(
            mutations.parse_keyword("budget app", "PHRASE"),
            ("budget app", "PHRASE"),
        )

    def test_check_texts_counts_lengths_and_deduplicates(self):
        self.assertEqual(
            mutations.check_texts("h", ["a", " a ", "b"], 1, 3, 30), ["a", "b"]
        )
        with self.assertRaisesRegex(ToolError, "between 3 and 15"):
            mutations.check_texts("headlines", ["a"], 3, 15, 30)
        with self.assertRaisesRegex(ToolError, "at most 30"):
            mutations.check_texts("headlines", ["x" * 31], 1, 15, 30)

    def test_format_date_time(self):
        self.assertEqual(
            mutations.format_date_time("2026-10-01"), "2026-10-01 00:00:00"
        )
        self.assertEqual(
            mutations.format_date_time("2026-10-01", end_of_day=True),
            "2026-10-01 23:59:59",
        )
        with self.assertRaises(ToolError):
            mutations.format_date_time("10/01/2026")

    def test_plan_changes(self):
        existing = {"a": "rn/a", "b": "rn/b"}
        self.assertEqual(
            mutations.plan_changes(existing, ["b", "c"], "add"), (["c"], [])
        )
        self.assertEqual(
            mutations.plan_changes(existing, ["b", "c"], "remove"),
            ([], ["rn/b"]),
        )
        self.assertEqual(
            mutations.plan_changes(existing, ["b", "c"], "replace"),
            (["c"], ["rn/a"]),
        )

    def test_mutate_groups_results_by_resource_type(self):
        client = make_client()
        response = client.get_type("MutateGoogleAdsResponse")
        for field, name in [
            ("campaign_budget_result", "customers/1/campaignBudgets/2"),
            ("campaign_result", "customers/1/campaigns/3"),
        ]:
            result = client.get_type("MutateOperationResponse")
            getattr(result, field).resource_name = name
            response.mutate_operation_responses.append(result)
        service = MagicMock()
        service.mutate.return_value = response

        with patch("ads_mcp.mutations.get_service", return_value=service):
            results = mutations.mutate(
                client, "1", [(client.get_type("MutateOperation"), "op")]
            )

        self.assertEqual(
            results,
            {
                "campaign_budget": ["customers/1/campaignBudgets/2"],
                "campaign": ["customers/1/campaigns/3"],
            },
        )

    def test_mutate_error_names_failed_operation(self):
        client = make_client()
        failure = client.get_type("GoogleAdsFailure")
        error = client.get_type("GoogleAdsError")
        error.message = "Too long."
        error.error_code.field_error = FieldErrorEnum.FieldError.REQUIRED
        for name, index in [("mutate_operations", 1), ("campaign", None)]:
            element = type(error.location).FieldPathElement(field_name=name)
            if index is not None:
                element.index = index
            error.location.field_path_elements.append(element)
        failure.errors.append(error)
        service = MagicMock()
        service.mutate.side_effect = GoogleAdsException(
            None, None, failure, "req-1"
        )

        with patch("ads_mcp.mutations.get_service", return_value=service):
            with self.assertRaises(ToolError) as ctx:
                mutations.mutate(
                    client,
                    "1",
                    [
                        (client.get_type("MutateOperation"), "budget"),
                        (client.get_type("MutateOperation"), "campaign 'X'"),
                    ],
                )

        message = str(ctx.exception)
        self.assertIn("req-1", message)
        self.assertIn("field_error.REQUIRED", message)
        self.assertIn("mutate_operations[1].campaign", message)
        self.assertIn("[operation: campaign 'X']", message)

    def test_parse_composite_id(self):
        self.assertEqual(
            mutations.parse_composite_id("2~3", "1", "adGroupAds", "ad"),
            ["2", "3"],
        )
        self.assertEqual(
            mutations.parse_composite_id(
                "customers/1/adGroupAds/2~3", "1", "adGroupAds", "ad"
            ),
            ["2", "3"],
        )
        with self.assertRaisesRegex(ToolError, "belongs to customer 9"):
            mutations.parse_composite_id(
                "customers/9/adGroupAds/2~3", "1", "adGroupAds", "ad"
            )
        with self.assertRaisesRegex(ToolError, "Expected customers"):
            mutations.parse_composite_id(
                "customers/1/campaigns/2", "1", "adGroupAds", "ad"
            )

    def test_last_id(self):
        self.assertEqual(mutations.last_id("customers/1/adGroupAds/2~3"), "3")
        self.assertEqual(mutations.last_id("customers/1/campaigns/5"), "5")


def policy_exception(client) -> GoogleAdsException:
    """A failure as the API reports policy problems on an ad and a keyword."""
    failure = client.get_type("GoogleAdsFailure")

    finding = client.get_type("GoogleAdsError")
    finding.message = "The resource has been disapproved."
    finding.error_code.policy_finding_error = (
        PolicyFindingErrorEnum.PolicyFindingError.POLICY_FINDING
    )
    entry = client.get_type("PolicyTopicEntry")
    entry.topic = "DESTINATION_MISMATCH"
    entry.type_ = client.enums.PolicyTopicEntryTypeEnum.PROHIBITED
    evidence = client.get_type("PolicyTopicEvidence")
    evidence.text_list.texts.append("example.org")
    entry.evidences.append(evidence)
    finding.details.policy_finding_details.policy_topic_entries.append(entry)
    failure.errors.append(finding)

    violation = client.get_type("GoogleAdsError")
    violation.message = "A policy was violated."
    violation.error_code.policy_violation_error = (
        PolicyViolationErrorEnum.PolicyViolationError.POLICY_ERROR
    )
    details = violation.details.policy_violation_details
    details.external_policy_name = "Healthcare and medicines"
    details.key.policy_name = "PHARMACY"
    details.key.violating_text = "cheap pills"
    details.is_exemptible = True
    failure.errors.append(violation)
    return GoogleAdsException(None, None, failure, "req-2")


class TestPolicyErrors(unittest.TestCase):

    def test_policy_topics_and_violations_are_named(self):
        client = make_client()
        message = mutations.format_google_ads_exception(
            policy_exception(client)
        )
        self.assertIn(
            "Policy: DESTINATION_MISMATCH (PROHIBITED): 'example.org'", message
        )
        self.assertIn(
            "Policy: Healthcare and medicines: 'cheap pills' (exemptible)",
            message,
        )
        self.assertIn("policy_finding_error.POLICY_FINDING", message)

    def test_errors_without_policy_details_are_unchanged(self):
        client = make_client()
        failure = client.get_type("GoogleAdsFailure")
        error = client.get_type("GoogleAdsError")
        error.message = "Too long."
        failure.errors.append(error)
        message = mutations.format_google_ads_exception(
            GoogleAdsException(None, None, failure, "req-3")
        )
        self.assertNotIn("Policy", message)


class TestFindAdGroup(MutateToolTestCase):

    def test_by_ad_group_id_within_campaign(self):
        self.search_results = [[{"ad_group.id": 7, "campaign.id": 5}]]
        row = mutations.find_ad_group(self.client, "1", "7", 5)
        self.assertEqual(row["ad_group.id"], 7)
        self.assertIn("ad_group.id = 7", self.queries[0])
        self.assertIn("campaign.id = 5", self.queries[0])
        self.assertIn("ad_group.status != 'REMOVED'", self.queries[0])

    def test_campaign_must_have_exactly_one_ad_group(self):
        self.search_results = [[{"ad_group.id": 7}]]
        row = mutations.find_ad_group(self.client, "1", None, 5)
        self.assertEqual(row["ad_group.id"], 7)
        self.search_results = [[{"ad_group.id": 7}, {"ad_group.id": 8}]]
        with self.assertRaisesRegex(ToolError, "has 2 ad groups"):
            mutations.find_ad_group(self.client, "1", None, 5)

    def test_missing_ad_group_and_arguments(self):
        with self.assertRaisesRegex(ToolError, "not found in campaign 5"):
            mutations.find_ad_group(self.client, "1", 7, 5)
        with self.assertRaisesRegex(ToolError, "Give ad_group_id"):
            mutations.find_ad_group(self.client, "1", None, None)
        with self.assertRaises(ToolError):
            mutations.find_ad_group(self.client, "1", "7 OR 1=1", None)


if __name__ == "__main__":
    unittest.main()
