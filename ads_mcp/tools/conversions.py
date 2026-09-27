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

"""Tools for conversion tracking: conversion actions and click conversion uploads."""

from typing import Any, Dict, List, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

import ads_mcp.mutations as mutations
import ads_mcp.utils as utils

conversions_mcp = FastMCP("conversions")

Category = Literal[
    "DEFAULT",
    "PAGE_VIEW",
    "PURCHASE",
    "SIGNUP",
    "DOWNLOAD",
    "ADD_TO_CART",
    "BEGIN_CHECKOUT",
    "SUBSCRIBE_PAID",
    "SUBMIT_LEAD_FORM",
    "IMPORTED_LEAD",
    "QUALIFIED_LEAD",
    "CONVERTED_LEAD",
    "BOOK_APPOINTMENT",
    "REQUEST_QUOTE",
    "CONTACT",
    "OUTBOUND_CLICK",
    "ENGAGEMENT",
]

# Categories where each click can lead to several valuable conversions.
_MANY_PER_CLICK_CATEGORIES = {"PURCHASE", "ADD_TO_CART", "BEGIN_CHECKOUT"}

_CONVERSION_ACTION_FIELDS = [
    "conversion_action.id",
    "conversion_action.name",
    "conversion_action.type",
    "conversion_action.status",
    "conversion_action.category",
    "conversion_action.origin",
    "conversion_action.primary_for_goal",
    "conversion_action.counting_type",
    "conversion_action.app_id",
    "conversion_action.value_settings.default_value",
    "conversion_action.value_settings.default_currency_code",
    "conversion_action.value_settings.always_use_default_value",
    "conversion_action.click_through_lookback_window_days",
]


@conversions_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_conversion_actions(
    customer_id: str | int,
    include_removed: bool = False,
    login_customer_id: str | int | None = None,
) -> List[Dict[str, Any]]:
    """Lists the account's conversion actions: what counts as a conversion, e.g. purchases,
    sign-ups or app installs.

    App conversions appear here once their source is linked to the account:
    Firebase and Google Analytics 4 (types FIREBASE_* and GOOGLE_ANALYTICS_4_*),
    Google Play (GOOGLE_PLAY_*), or third-party app analytics such as AppsFlyer
    or Adjust (THIRD_PARTY_APP_ANALYTICS_*). Use their IDs as
    conversion_action_ids in create_app_campaign.

    Args:
        customer_id: The Google Ads customer ID.
        include_removed: Whether to include removed conversion actions.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    query = (
        f"SELECT {', '.join(_CONVERSION_ACTION_FIELDS)} FROM conversion_action"
    )
    if not include_removed:
        query += " WHERE conversion_action.status != 'REMOVED'"
    return [
        {
            field.removeprefix("conversion_action."): v
            for field, v in row.items()
        }
        for row in mutations.search(client, customer_id, query)
    ]


@conversions_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_conversion_action(
    customer_id: str | int,
    conversion_action_id: str | int,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Returns a conversion action's settings, its tag snippets (for website conversions), and
    the conversions recorded in the last 30 days, to check that tracking works.

    Args:
        customer_id: The Google Ads customer ID.
        conversion_action_id: The conversion action ID.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = utils.clean_customer_id(customer_id)
    action_rn = mutations.resource_name(
        customer_id, "conversionActions", conversion_action_id
    )
    client = utils.get_googleads_client(login_customer_id=login_customer_id)

    rows = mutations.search(
        client,
        customer_id,
        f"SELECT {', '.join(_CONVERSION_ACTION_FIELDS)}, "
        "conversion_action.tag_snippets FROM conversion_action "
        f"WHERE conversion_action.resource_name = '{action_rn}'",
    )
    if not rows:
        raise ToolError(f"Conversion action {conversion_action_id} not found.")
    result = {
        field.removeprefix("conversion_action."): value
        for field, value in rows[0].items()
    }

    stats = mutations.search(
        client,
        customer_id,
        "SELECT segments.conversion_action, metrics.all_conversions, "
        "metrics.all_conversions_value FROM customer "
        f"WHERE segments.conversion_action = '{action_rn}' "
        "AND segments.date DURING LAST_30_DAYS",
    )
    result["last_30_days"] = {
        "conversions": sum(
            r.get("metrics.all_conversions") or 0 for r in stats
        ),
        "conversions_value": sum(
            r.get("metrics.all_conversions_value") or 0 for r in stats
        ),
    }
    return result


@conversions_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
def create_conversion_action(
    customer_id: str | int,
    name: str,
    category: Category,
    conversion_type: Literal["WEBPAGE", "UPLOAD_CLICKS"] = "WEBPAGE",
    default_value: float | None = None,
    currency_code: str | None = None,
    always_use_default_value: bool = False,
    counting_type: Literal["ONE_PER_CLICK", "MANY_PER_CLICK"] | None = None,
    click_through_lookback_window_days: int | None = None,
    primary_for_goal: bool = True,
    validate_only: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Creates a conversion action, to track a valuable action on a website or web app.

    Conversion types:
      - WEBPAGE: tracked with the Google tag on your site. The result includes
        the tag snippets to install on your pages.
      - UPLOAD_CLICKS: conversions your backend reports with
        upload_click_conversions, e.g. a sign-up or purchase recorded
        server-side, attributed with the gclid captured from the landing page URL.

    Mobile app conversions (installs and in-app events) are not created here:
    they are imported by linking Firebase, Google Analytics 4, Google Play or a
    third-party app analytics provider to the account in the Google Ads UI.

    Args:
        customer_id: The Google Ads customer ID.
        name: The conversion action name. Must be unique in the account.
        category: What the conversion represents, e.g. PURCHASE or SIGNUP.
        conversion_type: How conversions are reported, see above.
        default_value: The value of a conversion, used when no value is reported.
        currency_code: ISO 4217 currency of default_value, e.g. "USD".
          Defaults to the account currency.
        always_use_default_value: Always use default_value, ignoring reported values.
        counting_type: ONE_PER_CLICK (leads, sign-ups) or MANY_PER_CLICK
          (purchases). Defaults by category.
        click_through_lookback_window_days: Days after a click (1-90) in which
          conversions are attributed to it. Defaults to 30.
        primary_for_goal: Whether bidding optimizes for this conversion action.
        validate_only: If true, validates the request without creating anything.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        The new conversion action ID and, for WEBPAGE, its tag snippets.
    """
    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client(login_customer_id=login_customer_id)

    op = client.get_type("MutateOperation")
    action = op.conversion_action_operation.create
    action.name = name
    action.type_ = client.enums.ConversionActionTypeEnum[conversion_type]
    action.category = client.enums.ConversionActionCategoryEnum[category]
    action.status = client.enums.ConversionActionStatusEnum.ENABLED
    action.primary_for_goal = primary_for_goal
    if counting_type is None:
        counting_type = (
            "MANY_PER_CLICK"
            if category in _MANY_PER_CLICK_CATEGORIES
            else "ONE_PER_CLICK"
        )
    action.counting_type = client.enums.ConversionActionCountingTypeEnum[
        counting_type
    ]
    if click_through_lookback_window_days:
        action.click_through_lookback_window_days = (
            click_through_lookback_window_days
        )
    if default_value is not None:
        action.value_settings.default_value = default_value
    if currency_code:
        action.value_settings.default_currency_code = currency_code.upper()
    action.value_settings.always_use_default_value = always_use_default_value

    results = mutations.mutate(
        client,
        customer_id,
        [(op, f"conversion action '{name}'")],
        validate_only=validate_only,
    )
    if validate_only:
        return {"validate_only": True, "message": "The request is valid."}

    action_rn = results["conversion_action"][0]
    result: Dict[str, Any] = {
        "conversion_action_id": mutations.parse_id(action_rn),
        "resource_name": action_rn,
    }
    if conversion_type == "WEBPAGE":
        rows = mutations.search(
            client,
            customer_id,
            "SELECT conversion_action.tag_snippets FROM conversion_action "
            f"WHERE conversion_action.resource_name = '{action_rn}'",
        )
        if rows:
            result["tag_snippets"] = rows[0]["conversion_action.tag_snippets"]
    else:
        result["next_steps"] = (
            "Capture the gclid (or gbraid/wbraid) URL parameter on your landing "
            "page, store it with the user, and report conversions with "
            "upload_click_conversions. Uploads are accepted a few hours after "
            "the conversion action is created."
        )
    return result


@conversions_mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True
    )
)
def update_conversion_action(
    customer_id: str | int,
    conversion_action_id: str | int,
    name: str | None = None,
    status: Literal["ENABLED", "HIDDEN"] | None = None,
    primary_for_goal: bool | None = None,
    default_value: float | None = None,
    always_use_default_value: bool | None = None,
    validate_only: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Updates a conversion action. Only the given fields are changed.

    Set primary_for_goal to choose which conversions bidding optimizes for:
    primary actions are used for bidding, secondary ones are only reported.

    Args:
        customer_id: The Google Ads customer ID.
        conversion_action_id: The conversion action ID.
        name: A new name.
        status: ENABLED, or HIDDEN to stop showing it in the Google Ads UI.
        primary_for_goal: Whether bidding optimizes for this conversion action.
        default_value: The value of a conversion when no value is reported.
        always_use_default_value: Always use default_value, ignoring reported values.
        validate_only: If true, validates the request without changing anything.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        The updated fields.
    """
    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client(login_customer_id=login_customer_id)

    op = client.get_type("MutateOperation")
    action = op.conversion_action_operation.update
    action.resource_name = mutations.resource_name(
        customer_id, "conversionActions", conversion_action_id
    )
    changes: Dict[str, Any] = {}
    if name is not None:
        action.name = changes["name"] = name
    if status is not None:
        action.status = client.enums.ConversionActionStatusEnum[status]
        changes["status"] = status
    if primary_for_goal is not None:
        action.primary_for_goal = changes["primary_for_goal"] = primary_for_goal
    if default_value is not None:
        action.value_settings.default_value = default_value
        changes["value_settings.default_value"] = default_value
    if always_use_default_value is not None:
        action.value_settings.always_use_default_value = (
            always_use_default_value
        )
        changes["value_settings.always_use_default_value"] = (
            always_use_default_value
        )
    if not changes:
        raise ToolError("Provide at least one field to update.")

    mutations.update_mask(
        client, op.conversion_action_operation.update_mask, changes
    )
    mutations.mutate(
        client,
        customer_id,
        [(op, f"conversion action {conversion_action_id}")],
        validate_only=validate_only,
    )
    return {
        "conversion_action_id": mutations.parse_id(conversion_action_id),
        "updated": changes,
        "validate_only": validate_only,
    }


class ClickConversion(BaseModel):
    """A conversion to attribute to an ad click."""

    conversion_action_id: str | int = Field(
        description="ID of an UPLOAD_CLICKS conversion action."
    )
    conversion_date_time: str = Field(
        description='When the conversion happened, as "yyyy-mm-dd hh:mm:ss+|-hh:mm", '
        'e.g. "2026-09-27 14:05:00-07:00".'
    )
    gclid: str | None = Field(
        default=None,
        description="The Google click ID from the landing page URL.",
    )
    gbraid: str | None = Field(
        default=None,
        description="Click ID for app conversions from iOS web-to-app clicks.",
    )
    wbraid: str | None = Field(
        default=None,
        description="Click ID for web conversions from iOS app-to-web clicks.",
    )
    conversion_value: float | None = Field(
        default=None, description="The value of the conversion."
    )
    currency_code: str | None = Field(
        default=None, description="ISO 4217 currency of conversion_value."
    )
    order_id: str | None = Field(
        default=None,
        description="A unique ID for the conversion, used to deduplicate uploads.",
    )


@conversions_mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)
)
def upload_click_conversions(
    customer_id: str | int,
    conversions: List[ClickConversion],
    ad_user_data_consent: Literal["GRANTED", "DENIED"] | None = None,
    validate_only: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Uploads offline conversions reported by your backend, attributing them to ad clicks.

    Use with UPLOAD_CLICKS conversion actions (see create_conversion_action).
    Each conversion needs exactly one click ID: gclid, gbraid or wbraid.
    Conversions can be uploaded up to 90 days after the click. Each conversion
    is processed independently: the result lists which ones failed and why.

    Args:
        customer_id: The Google Ads customer ID.
        conversions: The conversions to upload (up to 2000).
        ad_user_data_consent: For users in the EEA, whether they consented to
          sending their data to Google for advertising.
        validate_only: If true, validates the request without uploading anything.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        The number of conversions uploaded, and errors for those that failed.
    """
    customer_id = utils.clean_customer_id(customer_id)
    if not 1 <= len(conversions) <= 2000:
        raise ToolError("Upload between 1 and 2000 conversions per request.")
    client = utils.get_googleads_client(login_customer_id=login_customer_id)

    request = client.get_type("UploadClickConversionsRequest")
    request.customer_id = customer_id
    request.partial_failure = True
    request.validate_only = validate_only
    for index, conversion in enumerate(conversions):
        click_ids = [
            c
            for c in (conversion.gclid, conversion.gbraid, conversion.wbraid)
            if c
        ]
        if len(click_ids) != 1:
            raise ToolError(
                f"conversions[{index}]: provide exactly one of gclid, gbraid or wbraid."
            )
        click_conversion = client.get_type("ClickConversion")
        click_conversion.conversion_action = mutations.resource_name(
            customer_id, "conversionActions", conversion.conversion_action_id
        )
        click_conversion.conversion_date_time = conversion.conversion_date_time
        for field in ("gclid", "gbraid", "wbraid", "order_id", "currency_code"):
            value = getattr(conversion, field)
            if value:
                setattr(click_conversion, field, value)
        if conversion.conversion_value is not None:
            click_conversion.conversion_value = conversion.conversion_value
        if ad_user_data_consent:
            click_conversion.consent.ad_user_data = (
                client.enums.ConsentStatusEnum[ad_user_data_consent]
            )
        request.conversions.append(click_conversion)

    service = mutations.get_service(client, "ConversionUploadService")
    try:
        response = service.upload_click_conversions(request=request)
    except GoogleAdsException as ex:
        raise ToolError(mutations.format_google_ads_exception(ex))

    errors = _partial_failure_errors(client, response.partial_failure_error)
    failed = {e["index"] for e in errors if e["index"] is not None}
    return {
        "uploaded": len(conversions) - len(failed),
        "failed": len(failed),
        "errors": errors,
        "validate_only": validate_only,
    }


def _partial_failure_errors(client, status) -> List[Dict[str, Any]]:
    """Extracts per-conversion errors from a partial failure status."""
    if not status or not status.details:
        return []
    failure_type = type(client.get_type("GoogleAdsFailure"))
    errors = []
    for detail in status.details:
        failure = failure_type.deserialize(detail.value)
        for error in failure.errors:
            index = next(
                (
                    element.index
                    for element in error.location.field_path_elements
                    if element.field_name == "conversions"
                ),
                None,
            )
            errors.append({"index": index, "message": error.message})
    return errors
