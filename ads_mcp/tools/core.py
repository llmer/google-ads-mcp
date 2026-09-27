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

"""Tools for exposing simple, core API methods to the MCP server."""

from typing import Any, Dict, List
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

import ads_mcp.mutations as mutations
import ads_mcp.utils as utils

from google.ads.googleads.v25.services.types.customer_service import (
    ListAccessibleCustomersResponse,
)

customers_mcp = FastMCP("customers")


@customers_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_accessible_customers() -> List[str]:
    """Returns ids of customers directly accessible by the user authenticating the call.

    Use this tool first to discover available customer IDs if the user hasn't
    provided one. Most other tools require a valid customer ID as input.

    Returns:
        List[str]: A list of customer IDs.
    """
    ga_service = utils.get_googleads_service("CustomerService")
    accessible_customers: ListAccessibleCustomersResponse = (
        ga_service.list_accessible_customers()
    )
    # remove customer/ from the start of each resource
    return [
        cust_rn.removeprefix("customers/")
        for cust_rn in accessible_customers.resource_names
    ]


@customers_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_accounts(include_client_accounts: bool = True) -> List[Dict[str, Any]]:
    """Lists the Google Ads accounts the user can access, with their name, currency, time zone
    and whether they are manager (MCC) accounts.

    Client accounts directly under an accessible manager account are included
    too. To work with such a client account, pass the returned
    `login_customer_id` along with its `customer_id` to other tools.

    Args:
        include_client_accounts: Whether to include client accounts of manager accounts.

    Returns:
        One entry per account. Accounts that cannot be read (e.g. cancelled
        ones) are returned with an `error`.
    """
    accounts: Dict[str, Dict[str, Any]] = {}
    for root_id in list_accessible_customers():
        client = utils.get_googleads_client(login_customer_id=root_id)
        max_level = 1 if include_client_accounts else 0
        try:
            rows = mutations.search(
                client,
                root_id,
                "SELECT customer_client.id, customer_client.descriptive_name, "
                "customer_client.currency_code, customer_client.time_zone, "
                "customer_client.manager, customer_client.test_account, "
                "customer_client.status, customer_client.level "
                "FROM customer_client "
                f"WHERE customer_client.level <= {max_level}",
            )
        except ToolError as e:
            accounts.setdefault(
                root_id, {"customer_id": root_id, "error": str(e)}
            )
            continue
        for row in rows:
            customer_id = str(row["customer_client.id"])
            level = row["customer_client.level"]
            # Prefer direct access over access through a manager.
            if customer_id in accounts and level > 0:
                continue
            accounts[customer_id] = {
                "customer_id": customer_id,
                "name": row["customer_client.descriptive_name"],
                "currency_code": row["customer_client.currency_code"],
                "time_zone": row["customer_client.time_zone"],
                "manager": row["customer_client.manager"],
                "test_account": row["customer_client.test_account"],
                "status": row["customer_client.status"],
                "login_customer_id": root_id,
                "directly_accessible": level == 0,
            }
    return list(accounts.values())
