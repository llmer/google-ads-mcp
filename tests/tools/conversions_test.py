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

"""Tests for the conversion tools."""

import unittest

from fastmcp.exceptions import ToolError

from ads_mcp.tools import conversions
from tests.tools.ads_fakes import MutateToolTestCase


class TestConversionActions(MutateToolTestCase):

    def test_create_webpage_action_returns_tag_snippets(self):
        snippets = [{"type_": "WEBPAGE", "event_snippet": "<script/>"}]
        self.search_results = [[{"conversion_action.tag_snippets": snippets}]]

        result = conversions.create_conversion_action(
            "1", "Sign-up", "SIGNUP", default_value=5, currency_code="usd"
        )

        [[operation]] = self.mutate_calls()
        action = operation.conversion_action_operation.create
        self.assertEqual(action.type_.name, "WEBPAGE")
        self.assertEqual(action.category.name, "SIGNUP")
        self.assertEqual(action.counting_type.name, "ONE_PER_CLICK")
        self.assertEqual(action.value_settings.default_value, 5)
        self.assertEqual(action.value_settings.default_currency_code, "USD")
        self.assertEqual(result["tag_snippets"], snippets)

    def test_purchase_counts_every_conversion(self):
        result = conversions.create_conversion_action(
            "1", "Purchase", "PURCHASE", conversion_type="UPLOAD_CLICKS"
        )
        [[operation]] = self.mutate_calls()
        action = operation.conversion_action_operation.create
        self.assertEqual(action.type_.name, "UPLOAD_CLICKS")
        self.assertEqual(action.counting_type.name, "MANY_PER_CLICK")
        self.assertIn("upload_click_conversions", result["next_steps"])

    def test_update_sets_mask_for_given_fields_only(self):
        conversions.update_conversion_action(
            "1", 42, primary_for_goal=False, default_value=10
        )
        [[operation]] = self.mutate_calls()
        update_op = operation.conversion_action_operation
        self.assertEqual(
            update_op.update.resource_name, "customers/1/conversionActions/42"
        )
        self.assertEqual(
            list(update_op.update_mask.paths),
            ["primary_for_goal", "value_settings.default_value"],
        )

    def test_update_requires_a_field(self):
        with self.assertRaises(ToolError):
            conversions.update_conversion_action("1", 42)


class TestUploadClickConversions(MutateToolTestCase):

    def _conversion(self, **kwargs):
        args = dict(
            conversion_action_id=42,
            conversion_date_time="2026-09-27 14:05:00-07:00",
            gclid="abc",
            conversion_value=19.99,
            currency_code="USD",
        )
        args.update(kwargs)
        return conversions.ClickConversion(**args)

    def test_uploads_with_partial_failure(self):
        self.service.upload_click_conversions.return_value = (
            self.client.get_type("UploadClickConversionsResponse")
        )
        result = conversions.upload_click_conversions(
            "1", [self._conversion()], ad_user_data_consent="GRANTED"
        )

        request = self.service.upload_click_conversions.call_args.kwargs[
            "request"
        ]
        self.assertTrue(request.partial_failure)
        [conversion] = request.conversions
        self.assertEqual(
            conversion.conversion_action, "customers/1/conversionActions/42"
        )
        self.assertEqual(conversion.gclid, "abc")
        self.assertEqual(conversion.conversion_value, 19.99)
        self.assertEqual(conversion.consent.ad_user_data.name, "GRANTED")
        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(result["errors"], [])

    def test_requires_exactly_one_click_id(self):
        with self.assertRaisesRegex(ToolError, "exactly one"):
            conversions.upload_click_conversions(
                "1", [self._conversion(gbraid="x")]
            )

    def test_reports_failed_conversions(self):
        failure = self.client.get_type("GoogleAdsFailure")
        error = self.client.get_type("GoogleAdsError")
        error.message = "The click is too old."
        element = type(error.location).FieldPathElement(
            field_name="conversions", index=1
        )
        error.location.field_path_elements.append(element)
        failure.errors.append(error)
        response = self.client.get_type("UploadClickConversionsResponse")
        detail = response.partial_failure_error.details.add()
        detail.value = type(failure).serialize(failure)
        self.service.upload_click_conversions.return_value = response

        result = conversions.upload_click_conversions(
            "1", [self._conversion(), self._conversion(gclid="old")]
        )

        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(
            result["errors"], [{"index": 1, "message": "The click is too old."}]
        )


class TestGetConversionAction(MutateToolTestCase):

    def test_returns_settings_snippets_and_recent_conversions(self):
        self.search_results = [
            [
                {
                    "conversion_action.name": "Sign-up",
                    "conversion_action.tag_snippets": [{"event_snippet": "x"}],
                }
            ],
            [
                {"metrics.all_conversions": 3.0},
                {"metrics.all_conversions": 2.0},
            ],
        ]
        result = conversions.get_conversion_action("1", 42)

        self.assertEqual(result["name"], "Sign-up")
        self.assertEqual(result["tag_snippets"], [{"event_snippet": "x"}])
        self.assertEqual(result["last_30_days"]["conversions"], 5.0)
        self.assertIn("customers/1/conversionActions/42", self.queries[1])


if __name__ == "__main__":
    unittest.main()
