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

"""Tools for campaign targeting: locations, languages, negative keywords and
Performance Max audience signals.

The set_* tools take a `mode`:
  - "add" (default): adds the given items, keeping existing ones.
  - "remove": removes the given items.
  - "replace": makes the targeting match exactly the given items.
"""

from typing import Any, Callable, Dict, List, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from mcp.types import ToolAnnotations

import ads_mcp.mutations as mutations
import ads_mcp.utils as utils

targeting_mcp = FastMCP("targeting")

Mode = Literal["add", "remove", "replace"]

_UPDATE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True
)


def _apply_changes(
    client,
    customer_id: str,
    to_create: List[Any],
    to_remove: List[str],
    build_create,
    operation_field: str,
    validate_only: bool,
) -> Dict[str, Any]:
    """Sends create and remove operations in a single request.

    Args:
        build_create: Function (new resource, key) filling in a created item.
        operation_field: The MutateOperation field, e.g. "campaign_criterion_operation".

    Returns:
        A summary of the items added and removed.
    """
    operations = []
    for resource_name in to_remove:
        op = client.get_type("MutateOperation")
        getattr(op, operation_field).remove = resource_name
        operations.append((op, f"remove {resource_name}"))
    for key in to_create:
        op = client.get_type("MutateOperation")
        build_create(getattr(op, operation_field).create, key)
        operations.append((op, f"create {key}"))
    if operations:
        mutations.mutate(client, customer_id, operations, validate_only)
    return {
        "added": [list(key) for key in to_create],
        "removed": to_remove,
        "validate_only": validate_only,
    }


def _set_campaign_criteria(
    client,
    customer_id: str,
    campaign_id: str,
    conditions: str,
    key_fields: List[str],
    desired: List[tuple],
    mode: str,
    set_key: Callable[[Any, tuple], None],
    validate_only: bool,
    check: Callable[[set], None] | None = None,
) -> Dict[str, Any]:
    """Diffs existing campaign criteria against `desired` and applies the changes.

    Args:
        conditions: GAQL conditions selecting the criteria to manage.
        key_fields: The campaign_criterion fields that identify an item.
        desired: The item keys given by the caller, as tuples of values of
          key_fields.
        set_key: Function (criterion, key) filling in a new criterion.
        check: Optional function called with the set of keys that would
          remain, which may raise to refuse the change.
    """
    rows = mutations.search(
        client,
        customer_id,
        f"SELECT campaign_criterion.resource_name, {', '.join(key_fields)} "
        f"FROM campaign_criterion WHERE campaign.id = {campaign_id} "
        f"AND campaign_criterion.status != 'REMOVED' AND {conditions}",
    )
    existing = {
        tuple(_normalize(f, row[f]) for f in key_fields): row[
            "campaign_criterion.resource_name"
        ]
        for row in rows
    }
    to_create, to_remove = mutations.plan_changes(existing, desired, mode)
    if check:
        removed = {key for key, rn in existing.items() if rn in to_remove}
        check((set(existing) - removed) | set(to_create))

    campaign_rn = mutations.resource_name(customer_id, "campaigns", campaign_id)

    def build(criterion, key):
        criterion.campaign = campaign_rn
        set_key(criterion, key)

    return _apply_changes(
        client,
        customer_id,
        to_create,
        to_remove,
        build,
        "campaign_criterion_operation",
        validate_only,
    )


def _normalize(field: str, value: Any) -> Any:
    """Google Ads compares keyword and search theme text case-insensitively."""
    return value.lower() if field.endswith(".text") else value


@targeting_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def find_geo_targets(
    location_names: List[str],
    country_code: str | None = None,
    locale: str = "en",
    login_customer_id: str | int | None = None,
) -> List[Dict[str, Any]]:
    """Finds location (geo target) IDs by name, for use in location targeting.

    Examples of IDs: 2840 United States, 2826 United Kingdom, 1023191 New York City.

    Args:
        location_names: Names of places to look up, e.g. ["California", "Toronto"].
        country_code: Optional ISO 3166-1 alpha-2 code to restrict results, e.g. "US".
        locale: Language of the returned names, e.g. "en".
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        Matching locations with their ID, name, canonical name, type and reach.
    """
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    service = mutations.get_service(client, "GeoTargetConstantService")
    request = client.get_type("SuggestGeoTargetConstantsRequest")
    request.locale = locale
    if country_code:
        request.country_code = country_code.upper()
    request.location_names.names.extend(location_names)
    try:
        response = service.suggest_geo_target_constants(request=request)
    except GoogleAdsException as ex:
        raise ToolError(mutations.format_google_ads_exception(ex))

    return [
        {
            "id": str(s.geo_target_constant.id),
            "name": s.geo_target_constant.name,
            "canonical_name": s.geo_target_constant.canonical_name,
            "target_type": s.geo_target_constant.target_type,
            "country_code": s.geo_target_constant.country_code,
            "status": s.geo_target_constant.status.name,
            "reach": s.reach,
            "search_term": s.search_term,
        }
        for s in response.geo_target_constant_suggestions
    ]


@targeting_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_audiences(
    customer_id: str | int,
    login_customer_id: str | int | None = None,
) -> List[Dict[str, Any]]:
    """Lists the account's audiences, whose IDs can be used as Performance Max audience signals.

    Audiences are created in the Google Ads UI (Tools > Audience manager) and
    combine your data segments (e.g. app users, site visitors), interests and
    demographics.

    Args:
        customer_id: The Google Ads customer ID.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    return [
        {
            "audience_id": str(row["audience.id"]),
            "name": row.get("audience.name"),
            "description": row.get("audience.description"),
        }
        for row in mutations.search(
            client,
            customer_id,
            "SELECT audience.id, audience.name, audience.description "
            "FROM audience WHERE audience.status = 'ENABLED'",
        )
    ]


@targeting_mcp.tool(annotations=_UPDATE)
def set_geo_targets(
    customer_id: str | int,
    campaign_id: str | int,
    location_ids: List[str | int] | None = None,
    excluded_location_ids: List[str | int] | None = None,
    mode: Mode = "add",
    validate_only: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Sets the locations a campaign targets or excludes.

    Find location IDs with find_geo_targets. mode "add" adds the given locations,
    "remove" removes them, and "replace" makes the campaign's location targeting
    exactly the given targeted and excluded locations. Removing all targeted
    locations is refused, since the campaign would then target all countries.

    Args:
        customer_id: The Google Ads customer ID.
        campaign_id: The campaign ID.
        location_ids: Location IDs to target, e.g. [2840] for the United States.
        excluded_location_ids: Location IDs to exclude.
        mode: add, remove or replace.
        validate_only: If true, validates the request without changing anything.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        The location criteria added and removed, as [geo target constant, excluded].
    """
    customer_id = utils.clean_customer_id(customer_id)
    campaign_id = mutations.parse_id(campaign_id, "campaign_id")
    desired = [
        (mutations.geo_target_constant(g), False) for g in location_ids or []
    ]
    desired += [
        (mutations.geo_target_constant(g), True)
        for g in excluded_location_ids or []
    ]
    if not desired:
        raise ToolError("Provide location_ids and/or excluded_location_ids.")
    client = utils.get_googleads_client(login_customer_id=login_customer_id)

    def set_key(criterion, key):
        criterion.location.geo_target_constant = key[0]
        criterion.negative = key[1]

    def check(remaining):
        if not any(not negative for _, negative in remaining):
            raise ToolError(
                "This would leave the campaign without targeted locations, so "
                "it would target all countries. Include at least one location_id."
            )

    return _set_campaign_criteria(
        client,
        customer_id,
        campaign_id,
        "campaign_criterion.type = 'LOCATION'",
        [
            "campaign_criterion.location.geo_target_constant",
            "campaign_criterion.negative",
        ],
        desired,
        mode,
        set_key,
        validate_only,
        check,
    )


@targeting_mcp.tool(annotations=_UPDATE)
def set_language_targets(
    customer_id: str | int,
    campaign_id: str | int,
    language_ids: List[str | int],
    mode: Mode = "add",
    validate_only: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Sets the languages a campaign targets.

    Common language IDs: 1000 English, 1003 Spanish, 1002 French, 1001 German,
    1005 Japanese, 1017 Chinese (simplified). A campaign without language
    targeting targets all languages.

    Args:
        customer_id: The Google Ads customer ID.
        campaign_id: The campaign ID.
        language_ids: Language IDs.
        mode: add, remove or replace.
        validate_only: If true, validates the request without changing anything.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        The language criteria added and removed.
    """
    customer_id = utils.clean_customer_id(customer_id)
    campaign_id = mutations.parse_id(campaign_id, "campaign_id")
    client = utils.get_googleads_client(login_customer_id=login_customer_id)

    def set_key(criterion, key):
        criterion.language.language_constant = key[0]

    return _set_campaign_criteria(
        client,
        customer_id,
        campaign_id,
        "campaign_criterion.type = 'LANGUAGE'",
        ["campaign_criterion.language.language_constant"],
        [(mutations.language_constant(i),) for i in language_ids],
        mode,
        set_key,
        validate_only,
    )


@targeting_mcp.tool(annotations=_UPDATE)
def set_negative_keywords(
    customer_id: str | int,
    campaign_id: str | int,
    keywords: List[str],
    mode: Mode = "add",
    validate_only: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Sets campaign-level negative keywords, which stop ads from showing for matching searches.

    Works for Search and Performance Max campaigns. Keyword syntax: "[free app]"
    is exact match, '"free app"' is phrase match, and bare text is broad match.
    With mode "replace", negative keywords not in the list are removed.

    Args:
        customer_id: The Google Ads customer ID.
        campaign_id: The campaign ID.
        keywords: Negative keywords.
        mode: add, remove or replace.
        validate_only: If true, validates the request without changing anything.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        The negative keywords added and removed, as [text, match type].
    """
    customer_id = utils.clean_customer_id(customer_id)
    campaign_id = mutations.parse_id(campaign_id, "campaign_id")
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    match_types = client.enums.KeywordMatchTypeEnum

    def set_key(criterion, key):
        criterion.negative = True
        criterion.keyword.text = key[0]
        criterion.keyword.match_type = match_types[key[1]]

    return _set_campaign_criteria(
        client,
        customer_id,
        campaign_id,
        "campaign_criterion.type = 'KEYWORD' "
        "AND campaign_criterion.negative = TRUE",
        [
            "campaign_criterion.keyword.text",
            "campaign_criterion.keyword.match_type",
        ],
        [mutations.parse_keyword(k, "BROAD") for k in keywords],
        mode,
        set_key,
        validate_only,
    )


@targeting_mcp.tool(annotations=_UPDATE)
def set_audience_signals(
    customer_id: str | int,
    asset_group_id: str | int,
    audience_ids: List[str | int] | None = None,
    search_themes: List[str] | None = None,
    mode: Mode = "add",
    validate_only: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Sets the audience signals of a Performance Max asset group.

    Signals are hints that help Google find converting users faster; they do
    not restrict who sees the ads. Two kinds are supported:
      - audiences: reusable Audience resources (combinations of your data,
        interests and demographics). Find their IDs by querying the `audience`
        resource with the search tool.
      - search themes: phrases describing what your customers search for,
        e.g. "habit tracker app".

    Args:
        customer_id: The Google Ads customer ID.
        asset_group_id: The asset group ID (see get_campaign).
        audience_ids: Audience IDs.
        search_themes: Search themes, max 80 characters each.
        mode: add, remove or replace. With replace, signals of both kinds that
          are not listed are removed.
        validate_only: If true, validates the request without changing anything.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        The signals added and removed.
    """
    customer_id = utils.clean_customer_id(customer_id)
    asset_group_id = mutations.parse_id(asset_group_id, "asset_group_id")
    client = utils.get_googleads_client(login_customer_id=login_customer_id)

    rows = mutations.search(
        client,
        customer_id,
        "SELECT asset_group_signal.resource_name, "
        "asset_group_signal.audience.audience, "
        "asset_group_signal.search_theme.text FROM asset_group_signal "
        f"WHERE asset_group.id = {asset_group_id}",
    )
    existing = {}
    for row in rows:
        if row.get("asset_group_signal.audience.audience"):
            key = ("audience", row["asset_group_signal.audience.audience"])
        elif row.get("asset_group_signal.search_theme.text"):
            key = (
                "search_theme",
                row["asset_group_signal.search_theme.text"].lower(),
            )
        else:
            # Other signal types are left untouched.
            continue
        existing[key] = row["asset_group_signal.resource_name"]

    desired = [
        ("audience", mutations.resource_name(customer_id, "audiences", a))
        for a in audience_ids or []
    ]
    # Compared case-insensitively, but created with the caller's casing.
    theme_text = {t.strip().lower(): t.strip() for t in search_themes or []}
    desired += [("search_theme", key) for key in theme_text]
    if not desired and mode != "replace":
        raise ToolError("Provide audience_ids and/or search_themes.")
    to_create, to_remove = mutations.plan_changes(existing, desired, mode)
    asset_group_rn = mutations.resource_name(
        customer_id, "assetGroups", asset_group_id
    )

    def build(signal, key):
        signal.asset_group = asset_group_rn
        kind, value = key
        if kind == "audience":
            signal.audience.audience = value
        else:
            signal.search_theme.text = theme_text[value]

    return _apply_changes(
        client,
        customer_id,
        to_create,
        to_remove,
        build,
        "asset_group_signal_operation",
        validate_only,
    )
