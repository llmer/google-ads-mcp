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

"""Shared helpers for tools that create or modify Google Ads entities.

All mutating tools send their operations through `GoogleAdsService.Mutate`, so
that related entities (budget, campaign, criteria, ads...) are created
atomically: either everything succeeds or nothing is created.
"""

import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from fastmcp.exceptions import ToolError
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.protobuf import field_mask_pb2

import ads_mcp.utils as utils
from ads_mcp.mcp_header_interceptor import MCPHeaderInterceptor

# Budgets and bids must be multiples of the currency's minimum unit. 10,000
# micros (0.01) is the minimum unit for most currencies.
_MICROS_ROUNDING = 10_000

# Result field suffix of MutateOperationResponse oneof members, e.g.
# "campaign_result".
_RESULT_SUFFIX = "_result"


class TempIds:
    """Hands out negative temporary IDs for use within a single mutate request.

    Temporary IDs let operations in the same request reference entities that
    are created earlier in that request.
    """

    def __init__(self):
        self._next = -1

    def resource_name(self, customer_id: str, collection: str) -> str:
        """Returns a resource name with a new temporary ID."""
        temp_id = self._next
        self._next -= 1
        return f"customers/{customer_id}/{collection}/{temp_id}"


def to_micros(amount: float | int | None) -> int | None:
    """Converts an amount in account currency units to micros.

    The result is rounded to the currency's minimum unit (0.01).
    """
    if amount is None:
        return None
    micros = Decimal(str(amount)) * 1_000_000 / _MICROS_ROUNDING
    return int(micros.quantize(Decimal(1), rounding=ROUND_HALF_UP)) * (
        _MICROS_ROUNDING
    )


def from_micros(micros: int | None) -> float | None:
    if micros is None:
        return None
    return int(micros) / 1_000_000


def parse_id(value: str | int, label: str = "id") -> str:
    """Returns the numeric ID from an ID or a resource name.

    Accepts values like `123`, `"123"` or `"customers/1/campaigns/123"`.
    """
    text = str(value).strip().rstrip("/")
    last = text.rsplit("/", 1)[-1]
    if not re.fullmatch(r"\d+", last):
        raise ToolError(f"Invalid {label}: '{value}'. Expected a numeric ID.")
    return last


def resource_name(customer_id: str, collection: str, value: str | int) -> str:
    """Builds a customer-scoped resource name, e.g. customers/1/assets/2."""
    return f"customers/{customer_id}/{collection}/{parse_id(value, collection)}"


def parse_composite_id(
    value: str, customer_id: str, collection: str, label: str
) -> List[str]:
    """Splits a composite ID into its parts.

    Accepts "2~3" or a resource name such as customers/1/adGroupAds/2~3, which
    must belong to `customer_id`. The caller validates each part.
    """
    text = str(value).strip()
    if "/" in text:
        match = re.fullmatch(rf"customers/(\d+)/{collection}/([^/]+)", text)
        if not match:
            raise ToolError(
                f"Invalid {label}: '{value}'. Expected "
                f"customers/<customer id>/{collection}/<id>."
            )
        if match.group(1) != customer_id:
            raise ToolError(
                f"{label} '{value}' belongs to customer {match.group(1)}, "
                f"not {customer_id}."
            )
        text = match.group(2)
    return text.split("~")


def last_id(resource: str) -> str:
    """Returns the last ID of a resource name, e.g. the ad ID of
    customers/1/adGroupAds/2~3."""
    return resource.rsplit("/", 1)[-1].split("~")[-1]


def geo_target_constant(value: str | int) -> str:
    return f"geoTargetConstants/{parse_id(value, 'geo target id')}"


def language_constant(value: str | int) -> str:
    return f"languageConstants/{parse_id(value, 'language id')}"


def format_date_time(date: str | None, end_of_day: bool = False) -> str | None:
    """Converts YYYY-MM-DD to the "yyyy-MM-dd HH:mm:ss" format campaigns use."""
    if not date:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ToolError(f"Invalid date '{date}'. Expected YYYY-MM-DD.")
    return f"{date} {'23:59:59' if end_of_day else '00:00:00'}"


def check_texts(
    label: str,
    texts: Sequence[str] | None,
    min_count: int,
    max_count: int,
    max_length: int,
) -> List[str]:
    """Validates the number and length of ad texts before calling the API.

    Length is checked with len(), a lower bound for Google Ads' own count
    (which counts wide characters twice), so valid text is never rejected.
    """
    # Duplicates would map to the same (deduplicated) asset and fail to link.
    texts = list(
        dict.fromkeys(t.strip() for t in texts or [] if t and t.strip())
    )
    if not min_count <= len(texts) <= max_count:
        raise ToolError(
            f"{label}: expected between {min_count} and {max_count} items, "
            f"got {len(texts)}."
        )
    too_long = [t for t in texts if len(t) > max_length]
    if too_long:
        raise ToolError(
            f"{label}: each item must be at most {max_length} characters. "
            f"Too long: {too_long}"
        )
    return texts


_MATCH_TYPE_SYNTAX = [
    (r"^\[(.+)\]$", "EXACT"),
    (r'^"(.+)"$', "PHRASE"),
]


def parse_keyword(keyword: str, default_match_type: str) -> Tuple[str, str]:
    """Parses keyword syntax into (text, match type).

    "[running shoes]" is EXACT, '"running shoes"' is PHRASE, and bare text
    uses `default_match_type`.
    """
    keyword = keyword.strip()
    for pattern, match_type in _MATCH_TYPE_SYNTAX:
        match = re.match(pattern, keyword)
        if match:
            return match.group(1).strip().lower(), match_type
    return keyword.lower(), default_match_type


def format_keyword(text: str, match_type: str) -> str:
    """Writes a keyword in match type syntax, the reverse of parse_keyword
    (with bare text for broad match)."""
    return {"EXACT": f"[{text}]", "PHRASE": f'"{text}"'}.get(match_type, text)


def criterion_label(row: Dict[str, Any]) -> str:
    """Describes an ad group criterion from a GAQL row, e.g.
    "[running shoes] in ad group 7"."""
    if row.get("ad_group_criterion.type", "KEYWORD") == "KEYWORD":
        what = format_keyword(
            row.get("ad_group_criterion.keyword.text", ""),
            row.get("ad_group_criterion.keyword.match_type", ""),
        )
    else:
        what = (
            f"{row['ad_group_criterion.type'].lower()} "
            f"{row.get('ad_group_criterion.criterion_id')}"
        )
    return f"{what} in ad group {row.get('ad_group.id')}"


def update_mask(client: GoogleAdsClient, target, paths: Iterable[str]):
    """Sets the update_mask of an update operation to the given field paths."""
    client.copy_from(target, field_mask_pb2.FieldMask(paths=list(paths)))


def _policy_details(error) -> str:
    """Describes the policy topics or violation reported with an error, if any."""
    parts = []
    for entry in error.details.policy_finding_details.policy_topic_entries:
        part = entry.topic
        if entry.type_:
            part += f" ({entry.type_.name})"
        evidence = [
            text
            for evidence in entry.evidences
            for text in evidence.text_list.texts
        ]
        if evidence:
            part += f": {', '.join(repr(text) for text in evidence)}"
        parts.append(part)
    violation = error.details.policy_violation_details
    name = violation.external_policy_name or violation.key.policy_name
    if name:
        part = name
        if violation.key.violating_text:
            part += f": {violation.key.violating_text!r}"
        if violation.is_exemptible:
            part += " (exemptible)"
        parts.append(part)
    return "; ".join(parts)


def format_google_ads_exception(
    ex: GoogleAdsException, labels: Sequence[str] | None = None
) -> str:
    """Turns a GoogleAdsException into a message an LLM can act on.

    Includes the error code and the field path of each error, the policy
    topics of policy errors, and when `labels` is given, a description of the
    operation that failed.
    """
    lines = [f"Request ID: {ex.request_id}"]
    for error in ex.failure.errors:
        code_field = error.error_code._pb.WhichOneof("error_code")
        code = ""
        if code_field:
            code = f"{code_field}.{getattr(error.error_code, code_field).name}"

        path_parts = []
        operation_index = None
        for element in error.location.field_path_elements:
            part = element.field_name
            if element._pb.HasField("index"):
                part += f"[{element.index}]"
                if element.field_name in ("mutate_operations", "operations"):
                    operation_index = element.index
            path_parts.append(part)

        line = f"Google Ads API Error: {error.message}"
        if code:
            line += f" ({code})"
        if path_parts:
            line += f" at {'.'.join(path_parts)}"
        if (
            labels is not None
            and operation_index is not None
            and operation_index < len(labels)
        ):
            line += f" [operation: {labels[operation_index]}]"
        policy = _policy_details(error)
        if policy:
            line += f" Policy: {policy}"
        lines.append(line)
    return "\n".join(lines)


def get_service(client: GoogleAdsClient, name: str):
    return client.get_service(name, interceptors=[MCPHeaderInterceptor()])


def mutate(
    client: GoogleAdsClient,
    customer_id: str,
    operations: Sequence[Tuple[Any, str]],
    validate_only: bool = False,
) -> Dict[str, List[str]]:
    """Sends MutateOperations atomically through GoogleAdsService.Mutate.

    Args:
        client: The Google Ads client.
        customer_id: The customer to mutate.
        operations: (MutateOperation, label) pairs. The label describes the
          operation and is used in error messages.
        validate_only: If true, the request is validated but not executed.

    Returns:
        The resource names that were created or modified, grouped by resource
        type, e.g. {"campaign": ["customers/1/campaigns/2"]}. Empty when
        validate_only is true.
    """
    labels = [label for _, label in operations]
    service = get_service(client, "GoogleAdsService")
    try:
        # validate_only is not a flattened argument of the generated client's
        # mutate(); it must travel on the request message.
        request = client.get_type("MutateGoogleAdsRequest")
        request.customer_id = customer_id
        request.mutate_operations.extend(op for op, _ in operations)
        request.validate_only = validate_only
        response = service.mutate(request=request)
    except GoogleAdsException as ex:
        raise ToolError(format_google_ads_exception(ex, labels))

    results: Dict[str, List[str]] = {}
    for operation_response in response.mutate_operation_responses:
        field = operation_response._pb.WhichOneof("response")
        if not field:
            continue
        name = field.removesuffix(_RESULT_SUFFIX)
        results.setdefault(name, []).append(
            getattr(operation_response, field).resource_name
        )
    return results


def search(
    client: GoogleAdsClient, customer_id: str, query: str
) -> List[Dict[str, Any]]:
    """Runs a GAQL query and returns rows as {field path: value} dicts."""
    utils.logger.info(f"ads_mcp.mutations query {query}")
    service = get_service(client, "GoogleAdsService")
    try:
        output = []
        for batch in service.search_stream(
            customer_id=customer_id, query=query
        ):
            for row in batch.results:
                output.append(
                    utils.format_output_row(row, batch.field_mask.paths)
                )
        return output
    except GoogleAdsException as ex:
        raise ToolError(format_google_ads_exception(ex))


def find_ad_group(
    client: GoogleAdsClient,
    customer_id: str,
    ad_group_id: str | int | None,
    campaign_id: str | int | None,
) -> Dict[str, Any]:
    """Returns the ad group to change: the given one, or the only ad group of
    the given campaign.

    The row has the ad group's id, name, status and default CPC bid, and its
    campaign's id, name, channel type and bidding strategy type.
    """
    if ad_group_id is None and campaign_id is None:
        raise ToolError(
            "Give ad_group_id, or campaign_id for a campaign with one ad group."
        )
    conditions = ["ad_group.status != 'REMOVED'"]
    if ad_group_id is not None:
        ad_group_id = parse_id(ad_group_id, "ad_group_id")
        conditions.append(f"ad_group.id = {ad_group_id}")
    if campaign_id is not None:
        campaign_id = parse_id(campaign_id, "campaign_id")
        conditions.append(f"campaign.id = {campaign_id}")
    rows = search(
        client,
        customer_id,
        "SELECT ad_group.id, ad_group.name, ad_group.status, "
        "ad_group.cpc_bid_micros, campaign.id, campaign.name, "
        "campaign.advertising_channel_type, campaign.bidding_strategy_type "
        "FROM ad_group "
        f"WHERE {' AND '.join(conditions)}",
    )
    if ad_group_id is not None:
        if not rows:
            scope = f" in campaign {campaign_id}" if campaign_id else ""
            raise ToolError(f"Ad group {ad_group_id} not found{scope}.")
        return rows[0]
    if len(rows) != 1:
        raise ToolError(
            f"Campaign {campaign_id} has {len(rows)} ad groups (or was not "
            "found); give ad_group_id instead. get_campaign lists them."
        )
    return rows[0]


def plan_changes(
    existing: Dict[Any, str],
    desired: Iterable[Any],
    mode: str,
) -> Tuple[List[Any], List[str]]:
    """Works out which items to create and which to remove.

    Args:
        existing: Map of item key to the resource name of the existing item.
        desired: The item keys given by the caller.
        mode: "add" creates missing items; "remove" removes the given items;
          "replace" makes the existing items match `desired` exactly.

    Returns:
        (keys to create, resource names to remove).
    """
    desired = list(dict.fromkeys(desired))
    if mode == "add":
        return [k for k in desired if k not in existing], []
    if mode == "remove":
        return [], [existing[k] for k in desired if k in existing]
    if mode == "replace":
        desired_set = set(desired)
        return (
            [k for k in desired if k not in existing],
            [rn for k, rn in existing.items() if k not in desired_set],
        )
    raise ToolError(f"Invalid mode '{mode}'. Use add, remove or replace.")
