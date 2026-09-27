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

"""Spend guardrails enforced by the server on tools that affect spend.

Limits are read from the `guardrails` section of tools_config.yaml, in the
account currency, and apply to every account unless overridden:

    guardrails:
      max_daily_budget: 100             # per campaign budget
      max_total_daily_budget: 500       # all enabled campaigns in an account
      max_budget_increase_percent: 50   # per update_budget call
      accounts:
        "1234567890":
          max_total_daily_budget: 2000

They are checked against live account data before any change is sent, and
cannot be bypassed through tool parameters.
"""

from dataclasses import asdict, dataclass
from typing import Any, Dict

from fastmcp.exceptions import ToolError

import ads_mcp.mutations as mutations
import ads_mcp.utils as utils
from ads_mcp.config import ToolsConfig

_LIMIT_FIELDS = (
    "max_daily_budget",
    "max_total_daily_budget",
    "max_budget_increase_percent",
)


@dataclass
class SpendLimits:
    """Spend limits for an account. None means no limit."""

    max_daily_budget: float | None = None
    max_total_daily_budget: float | None = None
    max_budget_increase_percent: float | None = None

    def as_dict(self) -> Dict[str, float | None]:
        return asdict(self)


def get_limits(customer_id: str) -> SpendLimits:
    """Returns the limits for an account, applying per-account overrides.

    The config is read on every call, so edits apply without a restart.
    """
    config = ToolsConfig.load().guardrails
    limits = {field: config.get(field) for field in _LIMIT_FIELDS}
    accounts = config.get("accounts") or {}
    for account_id, overrides in accounts.items():
        if utils.clean_customer_id(account_id) == customer_id:
            limits.update(
                {k: v for k, v in (overrides or {}).items() if k in limits}
            )
    return SpendLimits(**limits)


def check_daily_budget(limits: SpendLimits, daily_budget: float) -> None:
    """Refuses a single campaign budget above max_daily_budget."""
    if (
        limits.max_daily_budget is not None
        and daily_budget > limits.max_daily_budget
    ):
        raise ToolError(
            f"Guardrail: a daily budget of {daily_budget} exceeds the "
            f"configured max_daily_budget of {limits.max_daily_budget}."
        )


def check_budget_increase(
    limits: SpendLimits, current: float, new: float
) -> None:
    """Refuses budget increases larger than max_budget_increase_percent."""
    if (
        limits.max_budget_increase_percent is None
        or current <= 0
        or new <= current
    ):
        return
    increase = (new - current) / current * 100
    if increase > limits.max_budget_increase_percent:
        raise ToolError(
            f"Guardrail: raising the budget from {current} to {new} is a "
            f"{increase:.0f}% increase, above the configured "
            f"max_budget_increase_percent of "
            f"{limits.max_budget_increase_percent}%. Increase it in smaller steps."
        )


def enabled_budgets(client, customer_id: str) -> Dict[str, float]:
    """Returns the daily budgets used by enabled campaigns, by budget resource name.

    Shared budgets are counted once. Campaigns that have ended are excluded.
    """
    rows = mutations.search(
        client,
        customer_id,
        "SELECT campaign.campaign_budget, campaign_budget.amount_micros "
        "FROM campaign WHERE campaign.status = 'ENABLED' "
        "AND campaign.serving_status != 'ENDED'",
    )
    return {
        row["campaign.campaign_budget"]: mutations.from_micros(
            row["campaign_budget.amount_micros"]
        )
        or 0.0
        for row in rows
    }


def check_total_daily_budget(
    limits: SpendLimits,
    budgets: Dict[str, float],
    budget_rn: str,
    daily_budget: float,
) -> Dict[str, Any]:
    """Refuses changes that push enabled campaigns above max_total_daily_budget.

    Args:
        budgets: Current budgets of enabled campaigns (see enabled_budgets).
        budget_rn: The budget that will be enabled or changed.
        daily_budget: That budget's amount after the change.

    Returns:
        The current and projected totals.
    """
    current = sum(budgets.values())
    projected = current - budgets.get(budget_rn, 0.0) + daily_budget
    totals = {
        "enabled_daily_budget_total": round(current, 2),
        "projected_daily_budget_total": round(projected, 2),
    }
    if (
        limits.max_total_daily_budget is not None
        and projected > limits.max_total_daily_budget
        and projected > current
    ):
        raise ToolError(
            f"Guardrail: this would raise the total daily budget of enabled "
            f"campaigns from {current:.2f} to {projected:.2f}, above the "
            f"configured max_total_daily_budget of "
            f"{limits.max_total_daily_budget}. Lower or pause other "
            "campaigns first."
        )
    return totals
