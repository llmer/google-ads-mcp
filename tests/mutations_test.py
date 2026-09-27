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

import ads_mcp.mutations as mutations
from tests.tools.ads_fakes import make_client


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


if __name__ == "__main__":
    unittest.main()
