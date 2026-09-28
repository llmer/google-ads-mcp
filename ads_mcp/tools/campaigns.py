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

"""Tools for reading, creating and managing campaigns.

New campaigns are always created PAUSED so that nothing spends money until
the campaign is reviewed and explicitly enabled with `enable_campaign`.
"""

import uuid
from typing import Any, Dict, List, Literal, Tuple

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

import ads_mcp.guardrails as guardrails
import ads_mcp.mutations as mutations
import ads_mcp.utils as utils
from ads_mcp.tools import assets

campaigns_mcp = FastMCP("campaigns")

_CREATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
_UPDATE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True
)

_NEXT_STEPS = (
    "The campaign was created PAUSED. Review it with get_campaign, then call "
    "enable_campaign to start serving (and spending)."
)
_NO_GEO_WARNING = (
    "No location targeting was set, so the campaign targets all countries. "
    "Use set_geo_targets to restrict it."
)

_CAMPAIGN_FIELDS = [
    "campaign.id",
    "campaign.name",
    "campaign.status",
    "campaign.serving_status",
    "campaign.primary_status",
    "campaign.primary_status_reasons",
    "campaign.advertising_channel_type",
    "campaign.advertising_channel_sub_type",
    "campaign.bidding_strategy_type",
    "campaign.maximize_conversions.target_cpa_micros",
    "campaign.maximize_conversion_value.target_roas",
    "campaign.target_cpa.target_cpa_micros",
    "campaign.target_roas.target_roas",
    "campaign.start_date_time",
    "campaign.end_date_time",
    "campaign.network_settings.target_google_search",
    "campaign.network_settings.target_search_network",
    "campaign.network_settings.target_content_network",
    "campaign.app_campaign_setting.app_id",
    "campaign.app_campaign_setting.app_store",
    "campaign.app_campaign_setting.bidding_strategy_goal_type",
    "campaign.selective_optimization.conversion_actions",
    "campaign.brand_guidelines_enabled",
    "campaign_budget.id",
    "campaign_budget.amount_micros",
    "campaign_budget.explicitly_shared",
    "customer.currency_code",
]


def _campaign_rn(customer_id: str, campaign_id: str | int) -> str:
    return mutations.resource_name(customer_id, "campaigns", campaign_id)


def _new_campaign_operations(
    client,
    customer_id: str,
    ids: mutations.TempIds,
    name: str,
    daily_budget: float,
    start_date: str | None,
    end_date: str | None,
    contains_eu_political_advertising: bool,
) -> Tuple[List[Tuple[Any, str]], Any, str]:
    """Builds the budget and campaign operations shared by all campaign types.

    Returns:
        (operations, the campaign message to finish configuring, the
        campaign's temporary resource name).
    """
    if daily_budget is None or daily_budget <= 0:
        raise ToolError("daily_budget must be greater than 0.")
    guardrails.check_daily_budget(
        guardrails.get_limits(customer_id), daily_budget
    )

    budget_rn = ids.resource_name(customer_id, "campaignBudgets")
    budget_op = client.get_type("MutateOperation")
    budget = budget_op.campaign_budget_operation.create
    budget.resource_name = budget_rn
    budget.name = f"{name} budget #{uuid.uuid4().hex[:8]}"
    budget.amount_micros = mutations.to_micros(daily_budget)
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    # Performance Max and App campaigns cannot use shared budgets.
    budget.explicitly_shared = False

    campaign_rn = ids.resource_name(customer_id, "campaigns")
    campaign_op = client.get_type("MutateOperation")
    campaign = campaign_op.campaign_operation.create
    campaign.resource_name = campaign_rn
    campaign.name = name
    campaign.status = client.enums.CampaignStatusEnum.PAUSED
    campaign.campaign_budget = budget_rn
    eu_status = client.enums.EuPoliticalAdvertisingStatusEnum
    campaign.contains_eu_political_advertising = (
        eu_status.CONTAINS_EU_POLITICAL_ADVERTISING
        if contains_eu_political_advertising
        else eu_status.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
    )
    start = mutations.format_date_time(start_date)
    if start:
        campaign.start_date_time = start
    end = mutations.format_date_time(end_date, end_of_day=True)
    if end:
        campaign.end_date_time = end

    operations = [
        (budget_op, f"campaign budget ({daily_budget}/day)"),
        (campaign_op, f"campaign '{name}'"),
    ]
    return operations, campaign, campaign_rn


def _targeting_operations(
    client,
    campaign_rn: str,
    geo_target_ids: List[str | int] | None,
    excluded_geo_target_ids: List[str | int] | None,
    language_ids: List[str | int] | None,
) -> List[Tuple[Any, str]]:
    """Builds location and language criteria operations for a new campaign."""
    operations = []
    for geo_id, negative in [(g, False) for g in geo_target_ids or []] + [
        (g, True) for g in excluded_geo_target_ids or []
    ]:
        op = client.get_type("MutateOperation")
        criterion = op.campaign_criterion_operation.create
        criterion.campaign = campaign_rn
        criterion.location.geo_target_constant = mutations.geo_target_constant(
            geo_id
        )
        criterion.negative = negative
        operations.append(
            (op, f"{'excluded ' if negative else ''}location {geo_id}")
        )
    for language_id in language_ids or []:
        op = client.get_type("MutateOperation")
        criterion = op.campaign_criterion_operation.create
        criterion.campaign = campaign_rn
        criterion.language.language_constant = mutations.language_constant(
            language_id
        )
        operations.append((op, f"language {language_id}"))
    return operations


def _set_bidding(
    client,
    campaign,
    strategy: str,
    target_cpa: float | None = None,
    target_roas: float | None = None,
    max_cpc: float | None = None,
) -> None:
    """Sets a standard (non-portfolio) bidding strategy on a new campaign."""
    if strategy == "MAXIMIZE_CONVERSIONS":
        client.copy_from(
            campaign.maximize_conversions,
            client.get_type("MaximizeConversions"),
        )
        if target_cpa:
            campaign.maximize_conversions.target_cpa_micros = (
                mutations.to_micros(target_cpa)
            )
    elif strategy == "MAXIMIZE_CONVERSION_VALUE":
        client.copy_from(
            campaign.maximize_conversion_value,
            client.get_type("MaximizeConversionValue"),
        )
        if target_roas:
            campaign.maximize_conversion_value.target_roas = target_roas
    elif strategy == "MAXIMIZE_CLICKS":
        client.copy_from(campaign.target_spend, client.get_type("TargetSpend"))
        if max_cpc:
            campaign.target_spend.cpc_bid_ceiling_micros = mutations.to_micros(
                max_cpc
            )
    elif strategy == "MANUAL_CPC":
        client.copy_from(campaign.manual_cpc, client.get_type("ManualCpc"))
    else:
        raise ToolError(f"Unsupported bidding strategy '{strategy}'.")


def _creation_result(
    results: Dict[str, List[str]],
    validate_only: bool,
    warnings: List[str],
) -> Dict[str, Any]:
    if validate_only:
        result = {
            "validate_only": True,
            "message": "The request is valid. Nothing was created.",
        }
    else:
        result = {
            "campaign_id": mutations.parse_id(results["campaign"][0]),
            "status": "PAUSED",
            "created": results,
            "next_steps": _NEXT_STEPS,
        }
    if warnings:
        result["warnings"] = warnings
    return result


@campaigns_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_campaign(
    customer_id: str | int,
    campaign_id: str | int,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Returns the configuration of a campaign: status, budget, bidding, targeting and, for
    Performance Max campaigns, asset groups (or ad groups for other campaign types), plus
    performance over the last 30 days.

    Monetary values are in the account currency (not micros).

    Args:
        customer_id: The Google Ads customer ID.
        campaign_id: The campaign ID.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = utils.clean_customer_id(customer_id)
    campaign_id = mutations.parse_id(campaign_id, "campaign_id")
    client = utils.get_googleads_client(login_customer_id=login_customer_id)

    rows = mutations.search(
        client,
        customer_id,
        f"SELECT {', '.join(_CAMPAIGN_FIELDS)} FROM campaign "
        f"WHERE campaign.id = {campaign_id}",
    )
    if not rows:
        raise ToolError(f"Campaign {campaign_id} not found.")
    row = rows[0]

    def micros(field):
        value = row.get(field)
        return mutations.from_micros(value) if value else None

    campaign = {
        field.removeprefix("campaign."): value
        for field, value in row.items()
        if field.startswith("campaign.") and "micros" not in field
    }
    campaign["currency_code"] = row.get("customer.currency_code")
    campaign["daily_budget"] = micros("campaign_budget.amount_micros")
    campaign["budget_id"] = row.get("campaign_budget.id")
    campaign["budget_explicitly_shared"] = row.get(
        "campaign_budget.explicitly_shared"
    )
    campaign["target_cpa"] = micros(
        "campaign.target_cpa.target_cpa_micros"
    ) or micros("campaign.maximize_conversions.target_cpa_micros")

    campaign["targeting"] = mutations.search(
        client,
        customer_id,
        "SELECT campaign_criterion.criterion_id, campaign_criterion.type, "
        "campaign_criterion.negative, "
        "campaign_criterion.location.geo_target_constant, "
        "campaign_criterion.language.language_constant, "
        "campaign_criterion.keyword.text, campaign_criterion.keyword.match_type "
        "FROM campaign_criterion "
        f"WHERE campaign.id = {campaign_id} "
        "AND campaign_criterion.status != 'REMOVED' "
        "AND campaign_criterion.type IN ('LOCATION', 'LANGUAGE', 'KEYWORD')",
    )
    _add_location_names(client, customer_id, campaign["targeting"])

    if row["campaign.advertising_channel_type"] == "PERFORMANCE_MAX":
        campaign["asset_groups"] = mutations.search(
            client,
            customer_id,
            "SELECT asset_group.id, asset_group.name, asset_group.status, "
            "asset_group.primary_status, asset_group.ad_strength, "
            "asset_group.final_urls FROM asset_group "
            f"WHERE campaign.id = {campaign_id} "
            "AND asset_group.status != 'REMOVED'",
        )
    else:
        campaign["ad_groups"] = mutations.search(
            client,
            customer_id,
            "SELECT ad_group.id, ad_group.name, ad_group.status, "
            "ad_group.type FROM ad_group "
            f"WHERE campaign.id = {campaign_id} "
            "AND ad_group.status != 'REMOVED'",
        )

    metrics = mutations.search(
        client,
        customer_id,
        "SELECT metrics.impressions, metrics.clicks, metrics.cost_micros, "
        "metrics.conversions, metrics.conversions_value FROM campaign "
        f"WHERE campaign.id = {campaign_id} "
        "AND segments.date DURING LAST_30_DAYS",
    )
    if metrics:
        totals: Dict[str, float] = {}
        for metrics_row in metrics:
            for field, value in metrics_row.items():
                key = field.removeprefix("metrics.")
                totals[key] = totals.get(key, 0) + (value or 0)
        totals["cost"] = mutations.from_micros(totals.pop("cost_micros", 0))
        campaign["last_30_days"] = totals
    return campaign


def _add_location_names(client, customer_id: str, criteria: List[Dict]) -> None:
    """Adds the canonical name of each location criterion, e.g. "California,United States"."""
    geo_rns = sorted(
        {
            c["campaign_criterion.location.geo_target_constant"]
            for c in criteria
            if c.get("campaign_criterion.location.geo_target_constant")
        }
    )
    if not geo_rns:
        return
    rows = mutations.search(
        client,
        customer_id,
        "SELECT geo_target_constant.resource_name, "
        "geo_target_constant.canonical_name FROM geo_target_constant "
        "WHERE geo_target_constant.resource_name IN "
        f"({', '.join(repr(rn) for rn in geo_rns)})",
    )
    names = {
        row["geo_target_constant.resource_name"]: row[
            "geo_target_constant.canonical_name"
        ]
        for row in rows
    }
    for criterion in criteria:
        geo_rn = criterion.get(
            "campaign_criterion.location.geo_target_constant"
        )
        if geo_rn in names:
            criterion["location_name"] = names[geo_rn]


DateRange = Literal[
    "TODAY",
    "YESTERDAY",
    "LAST_7_DAYS",
    "LAST_14_DAYS",
    "LAST_30_DAYS",
    "THIS_MONTH",
    "LAST_MONTH",
]

_METRICS = [
    "metrics.impressions",
    "metrics.clicks",
    "metrics.cost_micros",
    "metrics.conversions",
    "metrics.conversions_value",
]


def _metrics_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    metrics = {
        field.removeprefix("metrics."): row.get(field) or 0
        for field in _METRICS
        if field != "metrics.cost_micros"
    }
    metrics["cost"] = mutations.from_micros(row.get("metrics.cost_micros") or 0)
    return metrics


@campaigns_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_campaigns(
    customer_id: str | int,
    statuses: List[Literal["ENABLED", "PAUSED", "REMOVED"]] | None = None,
    date_range: DateRange = "LAST_30_DAYS",
    login_customer_id: str | int | None = None,
) -> List[Dict[str, Any]]:
    """Lists campaigns with their type, status, bidding strategy, daily budget and performance.

    Use this to see what is running before creating or changing campaigns.
    Monetary values are in the account currency (not micros).

    Args:
        customer_id: The Google Ads customer ID.
        statuses: Campaign statuses to include. Defaults to ENABLED and PAUSED.
        date_range: The period for the performance metrics.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    status_list = ", ".join(
        f"'{status}'" for status in statuses or ["ENABLED", "PAUSED"]
    )
    rows = mutations.search(
        client,
        customer_id,
        "SELECT campaign.id, campaign.name, campaign.status, "
        "campaign.primary_status, campaign.serving_status, "
        "campaign.advertising_channel_type, "
        "campaign.advertising_channel_sub_type, "
        "campaign.bidding_strategy_type, campaign.start_date_time, "
        "campaign.end_date_time, campaign_budget.id, "
        "campaign_budget.amount_micros, campaign_budget.explicitly_shared "
        f"FROM campaign WHERE campaign.status IN ({status_list}) "
        "ORDER BY campaign.name",
    )
    # Metrics are fetched separately: campaigns without traffic in the period
    # would otherwise be missing from the list.
    metrics_rows = mutations.search(
        client,
        customer_id,
        f"SELECT campaign.id, {', '.join(_METRICS)} FROM campaign "
        f"WHERE campaign.status IN ({status_list}) "
        f"AND segments.date DURING {date_range}",
    )
    metrics = {row["campaign.id"]: _metrics_dict(row) for row in metrics_rows}
    empty_metrics = _metrics_dict({})

    campaigns = []
    for row in rows:
        campaign = {
            field.removeprefix("campaign."): value
            for field, value in row.items()
            if field.startswith("campaign.")
        }
        campaign["daily_budget"] = mutations.from_micros(
            row.get("campaign_budget.amount_micros")
        )
        campaign["budget_id"] = row.get("campaign_budget.id")
        campaign["budget_explicitly_shared"] = row.get(
            "campaign_budget.explicitly_shared"
        )
        campaign[date_range.lower()] = metrics.get(
            row["campaign.id"], empty_metrics
        )
        campaigns.append(campaign)
    return campaigns


@campaigns_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_spend_overview(
    customer_id: str | int,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Summarizes an account's committed and actual spend, and the spend guardrails in force.

    Call this before enabling campaigns or raising budgets. It returns:
      - the total daily budget of enabled campaigns (shared budgets counted once),
      - spend today and this month,
      - account-level spending limits from billing (account budgets), if any,
      - the server's spend guardrails and the headroom left under them.

    Google may spend up to 2x a campaign's daily budget on a given day, but
    charges at most 30.4x the daily budget per month.

    Args:
        customer_id: The Google Ads customer ID.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client(login_customer_id=login_customer_id)

    budgets = guardrails.enabled_budgets(client, customer_id)
    total_daily_budget = round(sum(budgets.values()), 2)

    customer = mutations.search(
        client,
        customer_id,
        "SELECT customer.descriptive_name, customer.currency_code, "
        "customer.time_zone FROM customer",
    )
    customer = customer[0] if customer else {}

    def cost(date_range: str) -> float:
        rows = mutations.search(
            client,
            customer_id,
            "SELECT metrics.cost_micros FROM customer "
            f"WHERE segments.date DURING {date_range}",
        )
        return mutations.from_micros(
            sum(row.get("metrics.cost_micros") or 0 for row in rows)
        )

    overview: Dict[str, Any] = {
        "customer_id": customer_id,
        "name": customer.get("customer.descriptive_name"),
        "currency_code": customer.get("customer.currency_code"),
        "time_zone": customer.get("customer.time_zone"),
        "enabled_campaign_budgets": len(budgets),
        "enabled_daily_budget_total": total_daily_budget,
        "max_possible_spend_today": round(total_daily_budget * 2, 2),
        "cost_today": cost("TODAY"),
        "cost_this_month": cost("THIS_MONTH"),
    }

    try:
        account_budgets = mutations.search(
            client,
            customer_id,
            "SELECT account_budget.name, account_budget.status, "
            "account_budget.approved_spending_limit_micros, "
            "account_budget.approved_spending_limit_type, "
            "account_budget.amount_served_micros, "
            "account_budget.approved_start_date_time, "
            "account_budget.approved_end_date_time "
            "FROM account_budget WHERE account_budget.status = 'APPROVED'",
        )
        overview["account_budgets"] = [
            {
                "name": row.get("account_budget.name"),
                "spending_limit": mutations.from_micros(
                    row.get("account_budget.approved_spending_limit_micros")
                )
                or row.get("account_budget.approved_spending_limit_type"),
                "amount_served": mutations.from_micros(
                    row.get("account_budget.amount_served_micros")
                ),
                "start": row.get("account_budget.approved_start_date_time"),
                "end": row.get("account_budget.approved_end_date_time"),
            }
            for row in account_budgets
        ]
    except ToolError as e:
        # Account budgets are only readable with billing access.
        overview["account_budgets_error"] = str(e)

    limits = guardrails.get_limits(customer_id)
    overview["guardrails"] = limits.as_dict()
    if limits.max_total_daily_budget is not None:
        overview["guardrails"]["daily_budget_headroom"] = round(
            limits.max_total_daily_budget - total_daily_budget, 2
        )
    return overview


@campaigns_mcp.tool(annotations=_CREATE)
def create_search_campaign(
    customer_id: str | int,
    name: str,
    daily_budget: float,
    final_url: str,
    headlines: List[str],
    descriptions: List[str],
    keywords: List[str],
    keyword_match_type: Literal["BROAD", "PHRASE", "EXACT"] = "PHRASE",
    negative_keywords: List[str] | None = None,
    geo_target_ids: List[str | int] | None = None,
    excluded_geo_target_ids: List[str | int] | None = None,
    language_ids: List[str | int] | None = None,
    bidding_strategy: Literal[
        "MAXIMIZE_CONVERSIONS",
        "MAXIMIZE_CONVERSION_VALUE",
        "MAXIMIZE_CLICKS",
        "MANUAL_CPC",
    ] = "MAXIMIZE_CONVERSIONS",
    target_cpa: float | None = None,
    target_roas: float | None = None,
    max_cpc: float | None = None,
    ad_group_name: str | None = None,
    path1: str | None = None,
    path2: str | None = None,
    include_search_partners: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
    contains_eu_political_advertising: bool = False,
    validate_only: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Creates a Google Search campaign, with a budget, targeting, one ad group, keywords and a
    responsive search ad, in a single atomic request. The campaign is created PAUSED.

    Use this to drive traffic to a website or web app. For mobile app installs
    use create_app_campaign instead.

    Look up location IDs with find_geo_targets (e.g. 2840 = United States).
    Common language IDs: 1000 English, 1003 Spanish, 1002 French, 1001 German.

    Keyword syntax: "[running shoes]" is exact match, '"running shoes"' is
    phrase match, and bare text uses keyword_match_type. Negative keywords use
    the same syntax, with bare text meaning broad match.

    Monetary amounts (daily_budget, target_cpa, max_cpc) are in the account
    currency, e.g. 25.5 means 25.50 USD for a USD account.

    Args:
        customer_id: The Google Ads customer ID.
        name: The campaign name. Must be unique in the account.
        daily_budget: Average daily budget in the account currency.
        final_url: The landing page URL for the ad.
        headlines: 3-15 headlines, max 30 characters each.
        descriptions: 2-4 descriptions, max 90 characters each.
        keywords: Keywords for the ad group.
        keyword_match_type: Match type for keywords without match type syntax.
        negative_keywords: Campaign-level negative keywords.
        geo_target_ids: Location IDs to target. If empty, all countries are targeted.
        excluded_geo_target_ids: Location IDs to exclude.
        language_ids: Language IDs to target. If empty, all languages are targeted.
        bidding_strategy: How to bid. MAXIMIZE_CONVERSIONS requires conversion tracking.
        target_cpa: Optional target cost per conversion, for MAXIMIZE_CONVERSIONS.
        target_roas: Optional target return on ad spend as a ratio (3.5 = 350%),
          for MAXIMIZE_CONVERSION_VALUE.
        max_cpc: Max cost per click. Required for MANUAL_CPC (the ad group's
          default bid); optional bid ceiling for MAXIMIZE_CLICKS.
        ad_group_name: The ad group name. Defaults to "<name> ad group".
        path1: Optional display URL path, max 15 characters.
        path2: Optional second display URL path, max 15 characters.
        include_search_partners: Whether to also show ads on Google search partner sites.
        start_date: Optional start date, YYYY-MM-DD. Defaults to today.
        end_date: Optional end date, YYYY-MM-DD.
        contains_eu_political_advertising: Whether the campaign contains EU political advertising.
        validate_only: If true, validates the request without creating anything.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        The new campaign ID and the resource names of all created entities.
    """
    customer_id = utils.clean_customer_id(customer_id)
    headlines = mutations.check_texts("headlines", headlines, 3, 15, 30)
    descriptions = mutations.check_texts("descriptions", descriptions, 2, 4, 90)
    if not keywords:
        raise ToolError("At least one keyword is required.")
    if bidding_strategy == "MANUAL_CPC" and not max_cpc:
        raise ToolError("max_cpc is required for MANUAL_CPC bidding.")

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    ids = mutations.TempIds()
    operations, campaign, campaign_rn = _new_campaign_operations(
        client,
        customer_id,
        ids,
        name,
        daily_budget,
        start_date,
        end_date,
        contains_eu_political_advertising,
    )
    campaign.advertising_channel_type = (
        client.enums.AdvertisingChannelTypeEnum.SEARCH
    )
    campaign.network_settings.target_google_search = True
    campaign.network_settings.target_search_network = include_search_partners
    campaign.network_settings.target_content_network = False
    campaign.network_settings.target_partner_search_network = False
    _set_bidding(
        client, campaign, bidding_strategy, target_cpa, target_roas, max_cpc
    )

    operations += _targeting_operations(
        client,
        campaign_rn,
        geo_target_ids,
        excluded_geo_target_ids,
        language_ids,
    )

    match_types = client.enums.KeywordMatchTypeEnum
    for keyword in negative_keywords or []:
        text, match_type = mutations.parse_keyword(keyword, "BROAD")
        op = client.get_type("MutateOperation")
        criterion = op.campaign_criterion_operation.create
        criterion.campaign = campaign_rn
        criterion.negative = True
        criterion.keyword.text = text
        criterion.keyword.match_type = match_types[match_type]
        operations.append((op, f"negative keyword '{keyword}'"))

    ad_group_rn = ids.resource_name(customer_id, "adGroups")
    op = client.get_type("MutateOperation")
    ad_group = op.ad_group_operation.create
    ad_group.resource_name = ad_group_rn
    ad_group.name = ad_group_name or f"{name} ad group"
    ad_group.campaign = campaign_rn
    ad_group.type_ = client.enums.AdGroupTypeEnum.SEARCH_STANDARD
    ad_group.status = client.enums.AdGroupStatusEnum.ENABLED
    if bidding_strategy == "MANUAL_CPC":
        ad_group.cpc_bid_micros = mutations.to_micros(max_cpc)
    operations.append((op, f"ad group '{ad_group.name}'"))

    for keyword in keywords:
        text, match_type = mutations.parse_keyword(keyword, keyword_match_type)
        op = client.get_type("MutateOperation")
        criterion = op.ad_group_criterion_operation.create
        criterion.ad_group = ad_group_rn
        criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        criterion.keyword.text = text
        criterion.keyword.match_type = match_types[match_type]
        operations.append((op, f"keyword '{keyword}'"))

    op = client.get_type("MutateOperation")
    ad_group_ad = op.ad_group_ad_operation.create
    ad_group_ad.ad_group = ad_group_rn
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
    ad_group_ad.ad.final_urls.append(final_url)
    for headline in headlines:
        ad_text = client.get_type("AdTextAsset")
        ad_text.text = headline
        ad_group_ad.ad.responsive_search_ad.headlines.append(ad_text)
    for description in descriptions:
        ad_text = client.get_type("AdTextAsset")
        ad_text.text = description
        ad_group_ad.ad.responsive_search_ad.descriptions.append(ad_text)
    if path1:
        ad_group_ad.ad.responsive_search_ad.path1 = path1
    if path2:
        ad_group_ad.ad.responsive_search_ad.path2 = path2
    operations.append((op, "responsive search ad"))

    results = mutations.mutate(
        client, customer_id, operations, validate_only=validate_only
    )
    warnings = [] if geo_target_ids else [_NO_GEO_WARNING]
    return _creation_result(results, validate_only, warnings)


@campaigns_mcp.tool(annotations=_CREATE)
def create_pmax_campaign(
    customer_id: str | int,
    name: str,
    daily_budget: float,
    final_url: str,
    headlines: List[str],
    long_headlines: List[str],
    descriptions: List[str],
    business_name: str,
    marketing_image_asset_ids: List[str | int],
    square_marketing_image_asset_ids: List[str | int],
    logo_asset_ids: List[str | int],
    portrait_marketing_image_asset_ids: List[str | int] | None = None,
    landscape_logo_asset_ids: List[str | int] | None = None,
    video_asset_ids: List[str | int] | None = None,
    asset_group_name: str | None = None,
    geo_target_ids: List[str | int] | None = None,
    excluded_geo_target_ids: List[str | int] | None = None,
    language_ids: List[str | int] | None = None,
    bidding_strategy: Literal[
        "MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE"
    ] = "MAXIMIZE_CONVERSIONS",
    target_cpa: float | None = None,
    target_roas: float | None = None,
    audience_ids: List[str | int] | None = None,
    search_themes: List[str] | None = None,
    brand_guidelines_enabled: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
    contains_eu_political_advertising: bool = False,
    validate_only: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Creates a Performance Max campaign with its budget, targeting and first asset group, in a
    single atomic request. The campaign is created PAUSED.

    Performance Max serves across Search, Display, YouTube, Discover, Gmail and
    Maps, and is suited to website conversion goals. It requires conversion
    tracking (see the conversions tools). For app installs use create_app_campaign.

    Upload images first with upload_image and upload_logo, and pass the returned
    asset IDs. Asset requirements:
      - 3-15 headlines (max 30 characters each)
      - 1-5 long headlines (max 90 characters each)
      - 2-5 descriptions (max 90 characters, at least one of max 60)
      - business_name, max 25 characters
      - 1+ landscape (1.91:1), 1+ square (1:1) marketing images and 1+ square logo
      - Videos are optional; Google may generate videos if none are provided.

    Monetary amounts are in the account currency, e.g. 25.5 means 25.50 USD.

    Args:
        customer_id: The Google Ads customer ID.
        name: The campaign name. Must be unique in the account.
        daily_budget: Average daily budget in the account currency.
        final_url: The landing page URL.
        headlines: Short headlines.
        long_headlines: Long headlines.
        descriptions: Descriptions.
        business_name: The advertiser or brand name.
        marketing_image_asset_ids: IDs of landscape (1.91:1) image assets.
        square_marketing_image_asset_ids: IDs of square (1:1) image assets.
        logo_asset_ids: IDs of square (1:1) logo image assets.
        portrait_marketing_image_asset_ids: IDs of portrait (4:5) image assets.
        landscape_logo_asset_ids: IDs of landscape (4:1) logo image assets.
        video_asset_ids: IDs of YouTube video assets (see create_youtube_video_assets).
        asset_group_name: The asset group name. Defaults to "<name> asset group".
        geo_target_ids: Location IDs to target (see find_geo_targets). If empty,
          all countries are targeted.
        excluded_geo_target_ids: Location IDs to exclude.
        language_ids: Language IDs to target, e.g. 1000 for English.
        bidding_strategy: MAXIMIZE_CONVERSIONS or MAXIMIZE_CONVERSION_VALUE.
        target_cpa: Optional target cost per conversion, for MAXIMIZE_CONVERSIONS.
        target_roas: Optional target return on ad spend as a ratio (3.5 = 350%),
          for MAXIMIZE_CONVERSION_VALUE.
        audience_ids: IDs of audiences to use as audience signals.
        search_themes: Search themes to use as signals, e.g. "budget tracking app".
        brand_guidelines_enabled: If true, the business name and logos are set at
          the campaign level and shared by all asset groups.
        start_date: Optional start date, YYYY-MM-DD. Defaults to today.
        end_date: Optional end date, YYYY-MM-DD.
        contains_eu_political_advertising: Whether the campaign contains EU political advertising.
        validate_only: If true, validates the request without creating the
          campaign. Headlines and descriptions are then validated inline.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        The new campaign ID and the resource names of all created entities.
    """
    customer_id = utils.clean_customer_id(customer_id)
    headlines, long_headlines, descriptions = assets.check_asset_group_inputs(
        headlines,
        long_headlines,
        descriptions,
        business_name,
        True,
        marketing_image_asset_ids,
        square_marketing_image_asset_ids,
        logo_asset_ids,
    )

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    ids = mutations.TempIds()
    operations, campaign, campaign_rn = _new_campaign_operations(
        client,
        customer_id,
        ids,
        name,
        daily_budget,
        start_date,
        end_date,
        contains_eu_political_advertising,
    )
    campaign.advertising_channel_type = (
        client.enums.AdvertisingChannelTypeEnum.PERFORMANCE_MAX
    )
    campaign.brand_guidelines_enabled = brand_guidelines_enabled
    _set_bidding(client, campaign, bidding_strategy, target_cpa, target_roas)

    operations += _targeting_operations(
        client,
        campaign_rn,
        geo_target_ids,
        excluded_geo_target_ids,
        language_ids,
    )
    operations += assets.build_asset_group_operations(
        client,
        customer_id,
        ids,
        campaign_rn,
        asset_group_name or f"{name} asset group",
        final_url,
        headlines,
        long_headlines,
        descriptions,
        business_name,
        {
            "MARKETING_IMAGE": marketing_image_asset_ids,
            "SQUARE_MARKETING_IMAGE": square_marketing_image_asset_ids,
            "PORTRAIT_MARKETING_IMAGE": portrait_marketing_image_asset_ids,
            "LOGO": logo_asset_ids,
            "LANDSCAPE_LOGO": landscape_logo_asset_ids,
            "YOUTUBE_VIDEO": video_asset_ids,
        },
        audience_ids,
        search_themes,
        brand_guidelines_enabled,
        validate_only,
    )

    results = mutations.mutate(
        client, customer_id, operations, validate_only=validate_only
    )
    warnings = [] if geo_target_ids else [_NO_GEO_WARNING]
    return _creation_result(results, validate_only, warnings)


# (goal, has target) -> app bidding goal type and campaign bidding strategy.
_APP_BIDDING = {
    ("INSTALLS", True): ("OPTIMIZE_INSTALLS_TARGET_INSTALL_COST", "TARGET_CPA"),
    ("INSTALLS", False): (
        "OPTIMIZE_INSTALLS_WITHOUT_TARGET_INSTALL_COST",
        "MAXIMIZE_CONVERSIONS",
    ),
    ("IN_APP_ACTIONS", True): (
        "OPTIMIZE_IN_APP_CONVERSIONS_TARGET_CONVERSION_COST",
        "TARGET_CPA",
    ),
    ("IN_APP_ACTIONS", False): (
        "OPTIMIZE_IN_APP_CONVERSIONS_WITHOUT_TARGET_CPA",
        "MAXIMIZE_CONVERSIONS",
    ),
    ("IN_APP_VALUE", True): (
        "OPTIMIZE_RETURN_ON_ADVERTISING_SPEND",
        "TARGET_ROAS",
    ),
    ("IN_APP_VALUE", False): (
        "OPTIMIZE_TOTAL_VALUE_WITHOUT_TARGET_ROAS",
        "MAXIMIZE_CONVERSION_VALUE",
    ),
}


@campaigns_mcp.tool(annotations=_CREATE)
def create_app_campaign(
    customer_id: str | int,
    name: str,
    daily_budget: float,
    app_id: str,
    app_store: Literal["GOOGLE_APP_STORE", "APPLE_APP_STORE"],
    headlines: List[str],
    descriptions: List[str],
    goal: Literal["INSTALLS", "IN_APP_ACTIONS", "IN_APP_VALUE"] = "INSTALLS",
    target_cpa: float | None = None,
    target_roas: float | None = None,
    conversion_action_ids: List[str | int] | None = None,
    image_asset_ids: List[str | int] | None = None,
    video_asset_ids: List[str | int] | None = None,
    geo_target_ids: List[str | int] | None = None,
    excluded_geo_target_ids: List[str | int] | None = None,
    language_ids: List[str | int] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    contains_eu_political_advertising: bool = False,
    validate_only: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Creates an App campaign to promote a mobile app (Android or iOS), with its budget,
    targeting, ad group and app ad, in a single atomic request. The campaign is created PAUSED.

    App campaigns serve across Google Play, Search, YouTube, Discover and the
    Display Network, and optimize towards installs or in-app actions.

    Goals:
      - INSTALLS: maximize installs. target_cpa is the target cost per install.
      - IN_APP_ACTIONS: maximize in-app conversions (e.g. sign-ups). Requires
        conversion_action_ids; target_cpa is the target cost per action.
      - IN_APP_VALUE: maximize conversion value (e.g. purchases). Requires
        conversion_action_ids; target_roas is the target return on ad spend.
    In-app conversion actions come from Firebase, Google Analytics 4, Google
    Play or a third-party app analytics provider linked to the account; find
    their IDs with list_conversion_actions.

    Monetary amounts are in the account currency, e.g. 25.5 means 25.50 USD.

    Args:
        customer_id: The Google Ads customer ID.
        name: The campaign name. Must be unique in the account.
        daily_budget: Average daily budget in the account currency.
        app_id: The app's store ID: the package name for Google Play
          (e.g. "com.example.app"), or the numeric App Store ID for iOS
          (e.g. "123456789").
        app_store: The app's store.
        headlines: 1-5 headlines, max 30 characters each.
        descriptions: 1-5 descriptions, max 90 characters each.
        goal: What to optimize for, see above.
        target_cpa: Optional target cost per install or in-app action.
        target_roas: Optional target return on ad spend as a ratio (3.5 = 350%),
          for IN_APP_VALUE.
        conversion_action_ids: In-app conversion actions to optimize for.
        image_asset_ids: Optional IDs of image assets (up to 20).
        video_asset_ids: Optional IDs of YouTube video assets (up to 20).
        geo_target_ids: Location IDs to target (see find_geo_targets). If empty,
          all countries are targeted.
        excluded_geo_target_ids: Location IDs to exclude.
        language_ids: Language IDs to target, e.g. 1000 for English.
        start_date: Optional start date, YYYY-MM-DD. Defaults to today.
        end_date: Optional end date, YYYY-MM-DD.
        contains_eu_political_advertising: Whether the campaign contains EU political advertising.
        validate_only: If true, validates the request without creating anything.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        The new campaign ID and the resource names of all created entities.
    """
    customer_id = utils.clean_customer_id(customer_id)
    headlines = mutations.check_texts("headlines", headlines, 1, 5, 30)
    descriptions = mutations.check_texts("descriptions", descriptions, 1, 5, 90)
    if goal != "INSTALLS" and not conversion_action_ids:
        raise ToolError(
            f"conversion_action_ids is required for the {goal} goal. "
            "Use list_conversion_actions to find in-app conversion actions."
        )
    if goal == "IN_APP_VALUE" and target_cpa:
        raise ToolError("Use target_roas, not target_cpa, for IN_APP_VALUE.")
    if goal != "IN_APP_VALUE" and target_roas:
        raise ToolError("target_roas is only supported for IN_APP_VALUE.")

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    ids = mutations.TempIds()
    operations, campaign, campaign_rn = _new_campaign_operations(
        client,
        customer_id,
        ids,
        name,
        daily_budget,
        start_date,
        end_date,
        contains_eu_political_advertising,
    )
    campaign.advertising_channel_type = (
        client.enums.AdvertisingChannelTypeEnum.MULTI_CHANNEL
    )
    campaign.advertising_channel_sub_type = (
        client.enums.AdvertisingChannelSubTypeEnum.APP_CAMPAIGN
    )
    campaign.app_campaign_setting.app_id = app_id
    campaign.app_campaign_setting.app_store = (
        client.enums.AppCampaignAppStoreEnum[app_store]
    )
    goal_type, strategy = _APP_BIDDING[(goal, bool(target_cpa or target_roas))]
    campaign.app_campaign_setting.bidding_strategy_goal_type = (
        client.enums.AppCampaignBiddingStrategyGoalTypeEnum[goal_type]
    )
    if strategy == "TARGET_CPA":
        campaign.target_cpa.target_cpa_micros = mutations.to_micros(target_cpa)
    elif strategy == "TARGET_ROAS":
        campaign.target_roas.target_roas = target_roas
    else:
        _set_bidding(client, campaign, strategy)
    for conversion_action_id in conversion_action_ids or []:
        campaign.selective_optimization.conversion_actions.append(
            mutations.resource_name(
                customer_id, "conversionActions", conversion_action_id
            )
        )

    operations += _targeting_operations(
        client,
        campaign_rn,
        geo_target_ids,
        excluded_geo_target_ids,
        language_ids,
    )

    ad_group_rn = ids.resource_name(customer_id, "adGroups")
    op = client.get_type("MutateOperation")
    ad_group = op.ad_group_operation.create
    ad_group.resource_name = ad_group_rn
    ad_group.name = f"{name} ad group"
    ad_group.campaign = campaign_rn
    ad_group.status = client.enums.AdGroupStatusEnum.ENABLED
    operations.append((op, f"ad group '{ad_group.name}'"))

    op = client.get_type("MutateOperation")
    ad_group_ad = op.ad_group_ad_operation.create
    ad_group_ad.ad_group = ad_group_rn
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
    app_ad = ad_group_ad.ad.app_ad
    for headline in headlines:
        ad_text = client.get_type("AdTextAsset")
        ad_text.text = headline
        app_ad.headlines.append(ad_text)
    for description in descriptions:
        ad_text = client.get_type("AdTextAsset")
        ad_text.text = description
        app_ad.descriptions.append(ad_text)
    for asset_id in image_asset_ids or []:
        image = client.get_type("AdImageAsset")
        image.asset = mutations.resource_name(customer_id, "assets", asset_id)
        app_ad.images.append(image)
    for asset_id in video_asset_ids or []:
        video = client.get_type("AdVideoAsset")
        video.asset = mutations.resource_name(customer_id, "assets", asset_id)
        app_ad.youtube_videos.append(video)
    operations.append((op, "app ad"))

    results = mutations.mutate(
        client, customer_id, operations, validate_only=validate_only
    )
    warnings = [] if geo_target_ids else [_NO_GEO_WARNING]
    return _creation_result(results, validate_only, warnings)


@campaigns_mcp.tool(annotations=_UPDATE)
def update_budget(
    customer_id: str | int,
    campaign_id: str | int,
    daily_budget: float,
    allow_shared_budget: bool = False,
    validate_only: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Changes the average daily budget of a campaign.

    Read the campaign first (get_campaign, or get_spend_overview for the whole
    account) and confirm budget increases with the user. Google may spend up
    to 2x the daily budget on a given day, but charges at most 30.4x it per month.

    If the campaign's budget is shared with other campaigns, the change affects
    all of them; this is refused unless allow_shared_budget is true. Spend
    guardrails configured on the server (max budget per campaign, max total
    daily budget of enabled campaigns, max increase per change) are enforced.

    Args:
        customer_id: The Google Ads customer ID.
        campaign_id: The campaign ID.
        daily_budget: The new average daily budget, in the account currency
          (e.g. 25.5 means 25.50 USD).
        allow_shared_budget: Allow changing a budget shared by several campaigns.
        validate_only: If true, validates the request without changing anything.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        The previous and new daily budget.
    """
    customer_id = utils.clean_customer_id(customer_id)
    campaign_id = mutations.parse_id(campaign_id, "campaign_id")
    if daily_budget is None or daily_budget <= 0:
        raise ToolError("daily_budget must be greater than 0.")
    client = utils.get_googleads_client(login_customer_id=login_customer_id)

    rows = mutations.search(
        client,
        customer_id,
        "SELECT campaign.campaign_budget, campaign_budget.amount_micros, "
        "campaign_budget.reference_count, customer.currency_code "
        f"FROM campaign WHERE campaign.id = {campaign_id}",
    )
    if not rows:
        raise ToolError(f"Campaign {campaign_id} not found.")
    row = rows[0]
    reference_count = row.get("campaign_budget.reference_count") or 0
    if reference_count > 1 and not allow_shared_budget:
        raise ToolError(
            f"This budget is shared by {reference_count} campaigns, which "
            "would all be affected. Set allow_shared_budget to proceed."
        )
    previous_budget = (
        mutations.from_micros(row.get("campaign_budget.amount_micros")) or 0.0
    )
    limits = guardrails.get_limits(customer_id)
    guardrails.check_daily_budget(limits, daily_budget)
    guardrails.check_budget_increase(limits, previous_budget, daily_budget)
    totals = guardrails.check_total_daily_budget(
        limits,
        guardrails.enabled_budgets(client, customer_id),
        row["campaign.campaign_budget"],
        daily_budget,
    )

    op = client.get_type("MutateOperation")
    budget = op.campaign_budget_operation.update
    budget.resource_name = row["campaign.campaign_budget"]
    budget.amount_micros = mutations.to_micros(daily_budget)
    mutations.update_mask(
        client, op.campaign_budget_operation.update_mask, ["amount_micros"]
    )
    mutations.mutate(
        client,
        customer_id,
        [(op, "campaign budget")],
        validate_only=validate_only,
    )
    return {
        "campaign_id": campaign_id,
        "currency_code": row.get("customer.currency_code"),
        "previous_daily_budget": previous_budget,
        "new_daily_budget": mutations.from_micros(budget.amount_micros),
        "campaigns_sharing_budget": reference_count,
        **totals,
        "validate_only": validate_only,
    }


def _set_campaign_status(
    customer_id: str | int,
    campaign_id: str | int,
    status: str,
    validate_only: bool,
    login_customer_id: str | int | None,
) -> Dict[str, Any]:
    customer_id = utils.clean_customer_id(customer_id)
    campaign_id = mutations.parse_id(campaign_id, "campaign_id")
    client = utils.get_googleads_client(login_customer_id=login_customer_id)

    rows = mutations.search(
        client,
        customer_id,
        "SELECT campaign.name, campaign.status, campaign.campaign_budget, "
        "campaign_budget.amount_micros, customer.currency_code "
        f"FROM campaign WHERE campaign.id = {campaign_id}",
    )
    if not rows:
        raise ToolError(f"Campaign {campaign_id} not found.")
    row = rows[0]
    daily_budget = (
        mutations.from_micros(row.get("campaign_budget.amount_micros")) or 0.0
    )
    totals = {}
    if status == "ENABLED":
        limits = guardrails.get_limits(customer_id)
        guardrails.check_daily_budget(limits, daily_budget)
        totals = guardrails.check_total_daily_budget(
            limits,
            guardrails.enabled_budgets(client, customer_id),
            row.get("campaign.campaign_budget"),
            daily_budget,
        )

    op = client.get_type("MutateOperation")
    campaign = op.campaign_operation.update
    campaign.resource_name = _campaign_rn(customer_id, campaign_id)
    campaign.status = client.enums.CampaignStatusEnum[status]
    mutations.update_mask(client, op.campaign_operation.update_mask, ["status"])
    mutations.mutate(
        client, customer_id, [(op, f"campaign {campaign_id}")], validate_only
    )
    return {
        "campaign_id": campaign_id,
        "name": row.get("campaign.name"),
        "previous_status": row.get("campaign.status"),
        "status": status,
        "daily_budget": daily_budget,
        "currency_code": row.get("customer.currency_code"),
        **totals,
        "validate_only": validate_only,
    }


@campaigns_mcp.tool(annotations=_UPDATE)
def pause_campaign(
    customer_id: str | int,
    campaign_id: str | int,
    validate_only: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Pauses a campaign, so that it stops serving ads and spending budget.

    Args:
        customer_id: The Google Ads customer ID.
        campaign_id: The campaign ID.
        validate_only: If true, validates the request without changing anything.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        The previous and new status.
    """
    return _set_campaign_status(
        customer_id, campaign_id, "PAUSED", validate_only, login_customer_id
    )


@campaigns_mcp.tool(annotations=_UPDATE)
def enable_campaign(
    customer_id: str | int,
    campaign_id: str | int,
    validate_only: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Enables a campaign, so that it starts serving ads and SPENDING its budget.

    Before enabling, review the campaign with get_campaign and the account's
    current spend with get_spend_overview, and confirm with the user. Refused
    if it would break the spend guardrails configured on the server.

    Args:
        customer_id: The Google Ads customer ID.
        campaign_id: The campaign ID.
        validate_only: If true, validates the request without changing anything.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        The previous and new status, and the campaign's daily budget.
    """
    return _set_campaign_status(
        customer_id, campaign_id, "ENABLED", validate_only, login_customer_id
    )


# Device criterion IDs are fixed by Google Ads.
_DEVICE_CRITERION_IDS = {"DESKTOP": 30000, "MOBILE": 30001, "TABLET": 30002}
_OPT = Literal["OPTED_IN", "OPTED_OUT"]


@campaigns_mcp.tool(annotations=_UPDATE)
def update_campaign_settings(
    customer_id: str | int,
    campaign_id: str | int,
    location_targeting: (
        Literal["PRESENCE", "PRESENCE_OR_INTEREST"] | None
    ) = None,
    device_bid_adjustments: Dict[str, float] | None = None,
    text_asset_automation: _OPT | None = None,
    final_url_expansion: _OPT | None = None,
    validate_only: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Updates campaign settings that are not about budget or targeting lists.

    Only the given settings are changed. None of them can raise a budget.

    Args:
        customer_id: The Google Ads customer ID.
        campaign_id: The campaign ID.
        location_targeting: PRESENCE shows ads only to people in or regularly
          in the targeted locations; PRESENCE_OR_INTEREST (Google's default)
          also includes people interested in them.
        device_bid_adjustments: Percent bid adjustments by device, e.g.
          {"DESKTOP": -50, "TABLET": -30, "MOBILE": 0}. -100 stops showing
          ads on that device. Range -100 to +100. Manual CPC and enhanced
          bidding use them; most Smart Bidding strategies ignore them.
        text_asset_automation: OPTED_OUT stops Google from generating
          headlines and descriptions from the landing page (Search, PMax).
        final_url_expansion: OPTED_OUT stops Google from sending clicks to
          other pages of the site than the ad's final URL.
        validate_only: If true, validates the request without changing anything.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        The settings that were changed.
    """
    customer_id = utils.clean_customer_id(customer_id)
    campaign_id = mutations.parse_id(campaign_id, "campaign_id")
    if not any(
        v is not None
        for v in (
            location_targeting,
            device_bid_adjustments,
            text_asset_automation,
            final_url_expansion,
        )
    ):
        raise ToolError("Nothing to update: give at least one setting.")
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    campaign_rn = _campaign_rn(customer_id, campaign_id)
    operations: List[Tuple[Any, str]] = []
    changed: Dict[str, Any] = {}

    paths: List[str] = []
    op = client.get_type("MutateOperation")
    campaign = op.campaign_operation.update
    campaign.resource_name = campaign_rn
    if location_targeting is not None:
        campaign.geo_target_type_setting.positive_geo_target_type = (
            client.enums.PositiveGeoTargetTypeEnum[location_targeting]
        )
        paths.append("geo_target_type_setting.positive_geo_target_type")
        changed["location_targeting"] = location_targeting
    automation = [
        ("TEXT_ASSET_AUTOMATION", text_asset_automation),
        ("FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION", final_url_expansion),
    ]
    if any(status is not None for _, status in automation):
        for automation_type, status in automation:
            if status is None:
                continue
            setting = client.get_type("Campaign").AssetAutomationSetting()
            setting.asset_automation_type = (
                client.enums.AssetAutomationTypeEnum[automation_type]
            )
            setting.asset_automation_status = (
                client.enums.AssetAutomationStatusEnum[status]
            )
            campaign.asset_automation_settings.append(setting)
        paths.append("asset_automation_settings")
        changed["text_asset_automation"] = text_asset_automation
        changed["final_url_expansion"] = final_url_expansion
    if paths:
        mutations.update_mask(client, op.campaign_operation.update_mask, paths)
        operations.append((op, f"campaign {campaign_id} settings"))

    if device_bid_adjustments:
        adjustments = {}
        for device, percent in device_bid_adjustments.items():
            key = device.upper()
            if key not in _DEVICE_CRITERION_IDS:
                raise ToolError(
                    f"Unknown device {device!r}; use DESKTOP, MOBILE or TABLET."
                )
            if not -100 <= percent <= 100:
                raise ToolError(
                    f"Bid adjustment for {key} must be between -100 and +100 percent."
                )
            adjustments[key] = percent
        rows = mutations.search(
            client,
            customer_id,
            "SELECT campaign_criterion.resource_name, campaign_criterion.device.type "
            f"FROM campaign_criterion WHERE campaign.id = {campaign_id} "
            "AND campaign_criterion.type = 'DEVICE'",
        )
        existing = {
            row["campaign_criterion.device.type"]: row[
                "campaign_criterion.resource_name"
            ]
            for row in rows
        }
        for device, percent in adjustments.items():
            op = client.get_type("MutateOperation")
            modifier = round(1 + percent / 100, 2)
            if device in existing:
                criterion = op.campaign_criterion_operation.update
                criterion.resource_name = existing[device]
                criterion.bid_modifier = modifier
                mutations.update_mask(
                    client,
                    op.campaign_criterion_operation.update_mask,
                    ["bid_modifier"],
                )
            else:
                criterion = op.campaign_criterion_operation.create
                criterion.campaign = campaign_rn
                criterion.device.type_ = client.enums.DeviceEnum[device]
                criterion.bid_modifier = modifier
            operations.append((op, f"device {device} bid adjustment"))
        changed["device_bid_adjustments"] = adjustments

    mutations.mutate(client, customer_id, operations, validate_only)
    return {
        "campaign_id": campaign_id,
        "changed": changed,
        "validate_only": validate_only,
    }


@campaigns_mcp.tool(annotations=_UPDATE)
def set_auto_apply_recommendations(
    customer_id: str | int,
    status: Literal["PAUSED", "ENABLED"] = "PAUSED",
    types: List[str] | None = None,
    validate_only: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Pauses (or re-enables) auto-applied recommendations for the whole account.

    Auto-apply lets Google change campaigns without review: add keywords,
    switch to broad match, turn on Display expansion, change bidding
    strategies or targets, and rewrite ads. These changes bypass this
    server's spend guardrails, so pausing them is recommended for accounts
    managed through these tools.

    Args:
        customer_id: The Google Ads customer ID.
        status: PAUSED (default) or ENABLED.
        types: Recommendation types to change, e.g. ["USE_BROAD_MATCH_KEYWORD"].
          Defaults to every subscription currently in the other status.
        validate_only: If true, validates the request without changing anything.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        The subscription types changed, and ones that could not be (types this
        API version reports as UNKNOWN must be changed in the Google Ads UI).
    """
    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    rows = mutations.search(
        client,
        customer_id,
        "SELECT recommendation_subscription.resource_name, "
        "recommendation_subscription.type, recommendation_subscription.status "
        "FROM recommendation_subscription",
    )
    wanted = {t.upper() for t in types} if types else None
    operations, changed, unsupported = [], [], 0
    for row in rows:
        rtype = row["recommendation_subscription.type"]
        if row["recommendation_subscription.status"] == status:
            continue
        if wanted is not None and rtype not in wanted:
            continue
        if rtype in ("UNKNOWN", "UNSPECIFIED"):
            unsupported += 1
            continue
        op = client.get_type("MutateOperation")
        sub = op.recommendation_subscription_operation.update
        sub.resource_name = row["recommendation_subscription.resource_name"]
        sub.status = client.enums.RecommendationSubscriptionStatusEnum[status]
        mutations.update_mask(
            client,
            op.recommendation_subscription_operation.update_mask,
            ["status"],
        )
        operations.append((op, f"auto-apply {rtype}"))
        changed.append(rtype)
    if operations:
        mutations.mutate(client, customer_id, operations, validate_only)
    return {
        "status": status,
        "changed": changed,
        "unsupported_unknown_types": unsupported,
        "validate_only": validate_only,
    }


@campaigns_mcp.tool(annotations=_UPDATE)
def set_cpc_bids(
    customer_id: str | int,
    campaign_id: str | int,
    default_cpc: float | None = None,
    keyword_bids: Dict[str, float] | None = None,
    validate_only: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Sets manual CPC bids for a Search campaign's ad group and keywords.

    For Manual CPC campaigns with one ad group (as created by
    create_search_campaign). Bids are in the account currency. Higher bids
    raise cost per click but never the budget; bids above the max_cpc_bid
    guardrail are refused.

    Args:
        customer_id: The Google Ads customer ID.
        campaign_id: The campaign ID.
        default_cpc: The ad group's default max CPC, used by keywords without
          their own bid.
        keyword_bids: Keyword-level max CPC, keyed by keyword in match type
          syntax: "[baby name app]" exact, '"baby name app"' phrase, bare text
          broad. Use 0 to clear a keyword bid (fall back to default_cpc).
        validate_only: If true, validates the request without changing anything.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        The bids before and after, per ad group and keyword.
    """
    customer_id = utils.clean_customer_id(customer_id)
    campaign_id = mutations.parse_id(campaign_id, "campaign_id")
    if default_cpc is None and not keyword_bids:
        raise ToolError("Give default_cpc and/or keyword_bids.")
    limits = guardrails.get_limits(customer_id)
    for bid in [default_cpc, *(keyword_bids or {}).values()]:
        if bid is None:
            continue
        if bid < 0:
            raise ToolError("Bids cannot be negative.")
        guardrails.check_cpc_bid(limits, bid)

    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    groups = mutations.search(
        client,
        customer_id,
        "SELECT ad_group.resource_name, ad_group.name, ad_group.cpc_bid_micros, "
        "campaign.bidding_strategy_type FROM ad_group "
        f"WHERE campaign.id = {campaign_id} AND ad_group.status != 'REMOVED'",
    )
    if len(groups) != 1:
        raise ToolError(
            f"Campaign {campaign_id} has {len(groups)} ad groups; this tool "
            "handles campaigns with exactly one."
        )
    group = groups[0]
    if group.get("campaign.bidding_strategy_type") != "MANUAL_CPC":
        raise ToolError("Manual bids only apply to MANUAL_CPC campaigns.")

    operations = []
    result: Dict[str, Any] = {
        "ad_group": group["ad_group.name"],
        "keywords": {},
    }
    if default_cpc is not None:
        op = client.get_type("MutateOperation")
        ag = op.ad_group_operation.update
        ag.resource_name = group["ad_group.resource_name"]
        ag.cpc_bid_micros = mutations.to_micros(default_cpc)
        mutations.update_mask(
            client, op.ad_group_operation.update_mask, ["cpc_bid_micros"]
        )
        operations.append((op, "ad group default CPC"))
        result["default_cpc"] = {
            "before": mutations.from_micros(
                group.get("ad_group.cpc_bid_micros")
            ),
            "after": default_cpc,
        }

    if keyword_bids:
        rows = mutations.search(
            client,
            customer_id,
            "SELECT ad_group_criterion.resource_name, ad_group_criterion.keyword.text, "
            "ad_group_criterion.keyword.match_type, ad_group_criterion.cpc_bid_micros "
            f"FROM ad_group_criterion WHERE campaign.id = {campaign_id} "
            "AND ad_group_criterion.type = 'KEYWORD' "
            "AND ad_group_criterion.negative = FALSE "
            "AND ad_group_criterion.status != 'REMOVED'",
        )
        existing = {
            (
                row["ad_group_criterion.keyword.text"].lower(),
                row["ad_group_criterion.keyword.match_type"],
            ): row
            for row in rows
        }
        for keyword, bid in keyword_bids.items():
            text, match_type = mutations.parse_keyword(keyword, "BROAD")
            row = existing.get((text.lower(), match_type))
            if row is None:
                raise ToolError(
                    f"Keyword {keyword!r} is not in campaign {campaign_id}."
                )
            op = client.get_type("MutateOperation")
            criterion = op.ad_group_criterion_operation.update
            criterion.resource_name = row["ad_group_criterion.resource_name"]
            criterion.cpc_bid_micros = mutations.to_micros(bid) if bid else 0
            mutations.update_mask(
                client,
                op.ad_group_criterion_operation.update_mask,
                ["cpc_bid_micros"],
            )
            operations.append((op, f"keyword {keyword}"))
            result["keywords"][keyword] = {
                "before": mutations.from_micros(
                    row.get("ad_group_criterion.cpc_bid_micros")
                ),
                "after": bid or None,
            }

    mutations.mutate(client, customer_id, operations, validate_only)
    result["validate_only"] = validate_only
    return result
