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

"""Test cases for core tools."""

import unittest
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError

from ads_mcp.tools import core


class TestCoreTools(unittest.TestCase):
    """Test cases for the core tools module."""

    @patch("ads_mcp.utils.get_googleads_service")
    def test_list_accessible_customers_success(self, mock_get_service):
        """Tests that list_accessible_customers returns customer IDs with prefix stripped."""
        mock_service = MagicMock()
        mock_response = MagicMock()
        mock_response.resource_names = [
            "customers/1234567890",
            "customers/9876543210",
        ]
        mock_service.list_accessible_customers.return_value = mock_response
        mock_get_service.return_value = mock_service

        result = core.list_accessible_customers()

        self.assertEqual(result, ["1234567890", "9876543210"])
        mock_get_service.assert_called_once_with("CustomerService")
        mock_service.list_accessible_customers.assert_called_once()

    @patch("ads_mcp.utils.get_googleads_client")
    @patch("ads_mcp.mutations.search")
    @patch("ads_mcp.tools.core.list_accessible_customers")
    def test_list_accounts_includes_clients_of_managers(
        self, mock_accessible, mock_search, mock_client
    ):
        """Tests that list_accounts expands managers and prefers direct access."""
        mock_accessible.return_value = ["100", "200", "300"]

        def row(customer_id, level, manager=False):
            return {
                "customer_client.id": customer_id,
                "customer_client.descriptive_name": f"Account {customer_id}",
                "customer_client.currency_code": "USD",
                "customer_client.time_zone": "America/New_York",
                "customer_client.manager": manager,
                "customer_client.test_account": False,
                "customer_client.status": "ENABLED",
                "customer_client.level": level,
            }

        mock_search.side_effect = [
            [row(100, 0, manager=True), row(200, 1), row(400, 1)],
            [row(200, 0)],
            ToolError("CUSTOMER_NOT_ENABLED"),
        ]

        accounts = {a["customer_id"]: a for a in core.list_accounts()}

        self.assertEqual(set(accounts), {"100", "200", "300", "400"})
        self.assertTrue(accounts["100"]["manager"])
        self.assertEqual(accounts["400"]["login_customer_id"], "100")
        self.assertFalse(accounts["400"]["directly_accessible"])
        self.assertEqual(accounts["200"]["login_customer_id"], "200")
        self.assertIn("CUSTOMER_NOT_ENABLED", accounts["300"]["error"])
        mock_client.assert_any_call(login_customer_id="100")


if __name__ == "__main__":
    unittest.main()
