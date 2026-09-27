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

"""Tools for creating assets and Performance Max asset groups."""

import base64
import binascii
import ipaddress
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Literal, Tuple

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

import ads_mcp.mutations as mutations
import ads_mcp.utils as utils

assets_mcp = FastMCP("assets")

# Google Ads rejects image assets larger than 5120 KB.
_MAX_IMAGE_BYTES = 5120 * 1024
_MAX_REDIRECTS = 5
_FETCH_TIMEOUT_SECONDS = 30

# Aspect ratios (width / height) and minimum sizes accepted for each image
# field type. See
# https://developers.google.com/google-ads/api/docs/performance-max/assets
_IMAGE_FIELD_SPECS = {
    "MARKETING_IMAGE": (1.91, 600, 314),
    "SQUARE_MARKETING_IMAGE": (1.0, 300, 300),
    "PORTRAIT_MARKETING_IMAGE": (0.8, 480, 600),
    "LOGO": (1.0, 128, 128),
    "LANDSCAPE_LOGO": (4.0, 512, 128),
}
_ASPECT_RATIO_TOLERANCE = 0.01

_CREATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)


def _is_hosted() -> bool:
    """True when the server runs as a shared web service (OAuth proxy mode)."""
    return bool(
        os.environ.get("GOOGLE_ADS_MCP_OAUTH_CLIENT_ID")
        and os.environ.get("GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET")
    )


def _check_public_host(url: str) -> None:
    """Rejects URLs that resolve to private, loopback or link-local addresses.

    Prevents the server from being used to fetch internal resources.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ToolError(f"Only http(s) image URLs are supported: {url}")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as e:
        raise ToolError(f"Could not resolve host of {url}: {e}")
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise ToolError(f"Refusing to fetch non-public address: {url}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _fetch_url(url: str) -> bytes:
    """Downloads an image, checking every redirect hop against _check_public_host."""
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(_MAX_REDIRECTS + 1):
        _check_public_host(url)
        request = urllib.request.Request(
            url, headers={"User-Agent": "google-ads-mcp"}
        )
        try:
            with opener.open(request, timeout=_FETCH_TIMEOUT_SECONDS) as resp:
                data = resp.read(_MAX_IMAGE_BYTES + 1)
                break
        except urllib.error.HTTPError as e:
            location = e.headers.get("Location")
            if e.code in (301, 302, 303, 307, 308) and location:
                url = urllib.parse.urljoin(url, location)
                continue
            raise ToolError(f"Failed to fetch image from {url}: HTTP {e.code}")
        except urllib.error.URLError as e:
            raise ToolError(f"Failed to fetch image from {url}: {e.reason}")
    else:
        raise ToolError(f"Too many redirects fetching {url}")
    if len(data) > _MAX_IMAGE_BYTES:
        raise ToolError("Image is larger than the 5120 KB Google Ads limit.")
    return data


def _load_image(
    image_url: str | None, image_base64: str | None, file_path: str | None
) -> Tuple[bytes, str]:
    """Returns the image bytes and a default name from exactly one source."""
    sources = [s for s in (image_url, image_base64, file_path) if s]
    if len(sources) != 1:
        raise ToolError(
            "Provide exactly one of image_url, image_base64 or file_path."
        )

    if image_url:
        data = _fetch_url(image_url)
        name = os.path.basename(urllib.parse.urlparse(image_url).path)
    elif image_base64:
        if image_base64.startswith("data:"):
            image_base64 = image_base64.split(",", 1)[-1]
        try:
            data = base64.b64decode(image_base64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ToolError(f"image_base64 is not valid base64: {e}")
        name = "image"
    else:
        if _is_hosted():
            raise ToolError(
                "file_path is not available when the server runs as a web "
                "service. Use image_url or image_base64 instead."
            )
        path = os.path.expanduser(file_path)
        if os.path.getsize(path) > _MAX_IMAGE_BYTES:
            raise ToolError(
                "Image is larger than the 5120 KB Google Ads limit."
            )
        with open(path, "rb") as f:
            data = f.read()
        name = os.path.basename(path)

    if len(data) > _MAX_IMAGE_BYTES:
        raise ToolError("Image is larger than the 5120 KB Google Ads limit.")
    return data, name or "image"


def suitable_image_field_types(width: int, height: int) -> List[str]:
    """Returns the image field types whose aspect ratio and size fit."""
    if not width or not height:
        return []
    ratio = width / height
    return [
        field_type
        for field_type, (target, min_w, min_h) in _IMAGE_FIELD_SPECS.items()
        if abs(ratio - target) / target <= _ASPECT_RATIO_TOLERANCE
        and width >= min_w
        and height >= min_h
    ]


def _upload_image_asset(
    customer_id: str | int,
    image_url: str | None,
    image_base64: str | None,
    file_path: str | None,
    name: str | None,
    validate_only: bool,
    login_customer_id: str | int | None,
) -> Dict[str, Any]:
    customer_id = utils.clean_customer_id(customer_id)
    data, default_name = _load_image(image_url, image_base64, file_path)
    client = utils.get_googleads_client(login_customer_id=login_customer_id)

    operation = client.get_type("MutateOperation")
    asset = operation.asset_operation.create
    # Image asset names must be unique within the account. When an image with
    # the same content already exists, the existing asset is returned.
    asset.name = name or f"{default_name} #{uuid.uuid4().hex[:8]}"
    asset.type_ = client.enums.AssetTypeEnum.IMAGE
    asset.image_asset.data = data

    results = mutations.mutate(
        client,
        customer_id,
        [(operation, f"image asset '{asset.name}'")],
        validate_only=validate_only,
    )
    if validate_only:
        return {"validate_only": True, "message": "Image is valid."}

    asset_rn = results["asset"][0]
    rows = mutations.search(
        client,
        customer_id,
        "SELECT asset.id, asset.name, asset.image_asset.full_size.width_pixels, "
        "asset.image_asset.full_size.height_pixels, asset.image_asset.mime_type "
        f"FROM asset WHERE asset.resource_name = '{asset_rn}'",
    )
    row = rows[0] if rows else {}
    width = row.get("asset.image_asset.full_size.width_pixels", 0)
    height = row.get("asset.image_asset.full_size.height_pixels", 0)
    return {
        "asset_id": mutations.parse_id(asset_rn),
        "resource_name": asset_rn,
        "name": row.get("asset.name", asset.name),
        "mime_type": row.get("asset.image_asset.mime_type"),
        "width": width,
        "height": height,
        "suitable_field_types": suitable_image_field_types(width, height),
    }


@assets_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_assets(
    customer_id: str | int,
    asset_types: List[Literal["IMAGE", "TEXT", "YOUTUBE_VIDEO"]] | None = None,
    limit: int = 200,
    login_customer_id: str | int | None = None,
) -> List[Dict[str, Any]]:
    """Lists the account's image, text and YouTube video assets, newest first.

    Check for existing assets before uploading new ones. Images include their
    dimensions and the image field types they fit.

    Args:
        customer_id: The Google Ads customer ID.
        asset_types: Asset types to include. Defaults to all three.
        limit: The maximum number of assets to return.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = utils.clean_customer_id(customer_id)
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    types = ", ".join(
        f"'{t}'" for t in asset_types or ["IMAGE", "TEXT", "YOUTUBE_VIDEO"]
    )
    rows = mutations.search(
        client,
        customer_id,
        "SELECT asset.id, asset.name, asset.type, asset.text_asset.text, "
        "asset.image_asset.full_size.width_pixels, "
        "asset.image_asset.full_size.height_pixels, "
        "asset.image_asset.full_size.url, "
        "asset.youtube_video_asset.youtube_video_id "
        f"FROM asset WHERE asset.type IN ({types}) "
        f"ORDER BY asset.id DESC LIMIT {int(limit)}",
    )
    output = []
    for row in rows:
        asset = {
            "asset_id": str(row["asset.id"]),
            "name": row.get("asset.name"),
            "type": row["asset.type"],
        }
        if row["asset.type"] == "TEXT":
            asset["text"] = row.get("asset.text_asset.text")
        elif row["asset.type"] == "IMAGE":
            width = row.get("asset.image_asset.full_size.width_pixels") or 0
            height = row.get("asset.image_asset.full_size.height_pixels") or 0
            asset.update(
                width=width,
                height=height,
                url=row.get("asset.image_asset.full_size.url"),
                suitable_field_types=suitable_image_field_types(width, height),
            )
        else:
            asset["youtube_video_id"] = row.get(
                "asset.youtube_video_asset.youtube_video_id"
            )
        output.append(asset)
    return output


@assets_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_asset_group(
    customer_id: str | int,
    asset_group_id: str | int,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Returns a Performance Max asset group: its status, ad strength, final URLs, linked assets
    grouped by field type (e.g. HEADLINE, MARKETING_IMAGE), and audience signals.

    Args:
        customer_id: The Google Ads customer ID.
        asset_group_id: The asset group ID (see get_campaign).
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.
    """
    customer_id = utils.clean_customer_id(customer_id)
    asset_group_id = mutations.parse_id(asset_group_id, "asset_group_id")
    client = utils.get_googleads_client(login_customer_id=login_customer_id)

    rows = mutations.search(
        client,
        customer_id,
        "SELECT asset_group.id, asset_group.name, asset_group.status, "
        "asset_group.primary_status, asset_group.primary_status_reasons, "
        "asset_group.ad_strength, asset_group.final_urls, campaign.id, "
        "campaign.name FROM asset_group "
        f"WHERE asset_group.id = {asset_group_id}",
    )
    if not rows:
        raise ToolError(f"Asset group {asset_group_id} not found.")
    result = {
        field.removeprefix("asset_group."): value
        for field, value in rows[0].items()
    }

    assets_by_field: Dict[str, List[Dict[str, Any]]] = {}
    for row in mutations.search(
        client,
        customer_id,
        "SELECT asset_group_asset.field_type, asset_group_asset.status, "
        "asset_group_asset.primary_status, asset.id, asset.name, asset.type, "
        "asset.text_asset.text, asset.youtube_video_asset.youtube_video_id "
        "FROM asset_group_asset "
        f"WHERE asset_group.id = {asset_group_id} "
        "AND asset_group_asset.status != 'REMOVED'",
    ):
        asset = {
            "asset_id": str(row["asset.id"]),
            "status": row.get("asset_group_asset.status"),
            "primary_status": row.get("asset_group_asset.primary_status"),
        }
        if row.get("asset.text_asset.text"):
            asset["text"] = row["asset.text_asset.text"]
        elif row.get("asset.youtube_video_asset.youtube_video_id"):
            asset["youtube_video_id"] = row[
                "asset.youtube_video_asset.youtube_video_id"
            ]
        else:
            asset["name"] = row.get("asset.name")
        assets_by_field.setdefault(
            row["asset_group_asset.field_type"], []
        ).append(asset)
    result["assets"] = assets_by_field

    result["signals"] = [
        {k.removeprefix("asset_group_signal."): v for k, v in row.items() if v}
        for row in mutations.search(
            client,
            customer_id,
            "SELECT asset_group_signal.audience.audience, "
            "asset_group_signal.search_theme.text, "
            "asset_group_signal.approval_status FROM asset_group_signal "
            f"WHERE asset_group.id = {asset_group_id}",
        )
    ]
    return result


@assets_mcp.tool(annotations=_CREATE)
def upload_image(
    customer_id: str | int,
    image_url: str | None = None,
    image_base64: str | None = None,
    file_path: str | None = None,
    name: str | None = None,
    validate_only: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Uploads an image as an image asset, for use in Performance Max asset groups and App campaigns.

    Provide exactly one image source. Images must be at most 5120 KB, and in
    JPEG, PNG or GIF format.

    The result reports the image dimensions and which image field types the
    image fits (by aspect ratio and minimum size):
      - MARKETING_IMAGE: landscape 1.91:1, min 600x314 (1200x628 recommended)
      - SQUARE_MARKETING_IMAGE: 1:1, min 300x300 (1200x1200 recommended)
      - PORTRAIT_MARKETING_IMAGE: 4:5, min 480x600 (960x1200 recommended)
    Use upload_logo for logos.

    Args:
        customer_id: The Google Ads customer ID.
        image_url: A public http(s) URL to download the image from.
        image_base64: The image content, base64 encoded (a data: URL is also accepted).
        file_path: A path to an image on the machine running the server. Not
          available when the server runs as a web service.
        name: Optional unique name for the asset. Defaults to the file name.
        validate_only: If true, validates the request without creating anything.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        The asset ID and resource name, the image dimensions and suitable field types.
    """
    return _upload_image_asset(
        customer_id,
        image_url,
        image_base64,
        file_path,
        name,
        validate_only,
        login_customer_id,
    )


@assets_mcp.tool(annotations=_CREATE)
def upload_logo(
    customer_id: str | int,
    image_url: str | None = None,
    image_base64: str | None = None,
    file_path: str | None = None,
    name: str | None = None,
    validate_only: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Uploads a logo image as an image asset.

    Logos must be square (1:1, min 128x128, 1200x1200 recommended) for the
    LOGO field, or 4:1 (min 512x128, 1200x300 recommended) for LANDSCAPE_LOGO.
    Provide exactly one image source. The result includes a warning if the
    image does not fit either logo format.

    Args:
        customer_id: The Google Ads customer ID.
        image_url: A public http(s) URL to download the logo from.
        image_base64: The logo content, base64 encoded (a data: URL is also accepted).
        file_path: A path to an image on the machine running the server. Not
          available when the server runs as a web service.
        name: Optional unique name for the asset. Defaults to the file name.
        validate_only: If true, validates the request without creating anything.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        The asset ID and resource name, the image dimensions and suitable field types.
    """
    result = _upload_image_asset(
        customer_id,
        image_url,
        image_base64,
        file_path,
        name,
        validate_only,
        login_customer_id,
    )
    fits = result.get("suitable_field_types")
    if fits is not None and not {"LOGO", "LANDSCAPE_LOGO"} & set(fits):
        result["warning"] = (
            f"The image ({result['width']}x{result['height']}) does not fit "
            "the LOGO (1:1, min 128x128) or LANDSCAPE_LOGO (4:1, min 512x128) "
            "formats. The asset was created but will be rejected as a logo."
        )
    return result


@assets_mcp.tool(annotations=_CREATE)
def create_text_assets(
    customer_id: str | int,
    texts: List[str],
    validate_only: bool = False,
    login_customer_id: str | int | None = None,
) -> List[Dict[str, str]]:
    """Creates text assets, e.g. headlines and descriptions, and returns their IDs.

    Creating a text asset that already exists returns the existing asset.
    Campaign and asset group creation tools accept text directly, so this is
    only needed to build reusable assets.

    Args:
        customer_id: The Google Ads customer ID.
        texts: The texts to create assets for.
        validate_only: If true, validates the request without creating anything.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        A list of {text, asset_id, resource_name}.
    """
    customer_id = utils.clean_customer_id(customer_id)
    texts = mutations.check_texts("texts", texts, 1, 1000, 1000)
    client = utils.get_googleads_client(login_customer_id=login_customer_id)
    resource_names = _create_text_assets(
        client, customer_id, texts, validate_only
    )
    if validate_only:
        return [{"text": t, "validate_only": "valid"} for t in texts]
    return [
        {
            "text": text,
            "asset_id": mutations.parse_id(rn),
            "resource_name": rn,
        }
        for text, rn in zip(texts, resource_names)
    ]


@assets_mcp.tool(annotations=_CREATE)
def create_youtube_video_assets(
    customer_id: str | int,
    youtube_video_ids: List[str],
    validate_only: bool = False,
    login_customer_id: str | int | None = None,
) -> List[Dict[str, str]]:
    """Creates video assets from YouTube videos, for Performance Max asset groups and App campaigns.

    The videos must be public or unlisted on YouTube.

    Args:
        customer_id: The Google Ads customer ID.
        youtube_video_ids: YouTube video IDs, e.g. "dQw4w9WgXcQ" (the `v=` part of a
          YouTube URL). Full YouTube URLs are also accepted.
        validate_only: If true, validates the request without creating anything.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        A list of {youtube_video_id, asset_id, resource_name}.
    """
    customer_id = utils.clean_customer_id(customer_id)
    video_ids = [_youtube_video_id(v) for v in youtube_video_ids]
    client = utils.get_googleads_client(login_customer_id=login_customer_id)

    operations = []
    for video_id in video_ids:
        operation = client.get_type("MutateOperation")
        asset = operation.asset_operation.create
        asset.name = f"YouTube video {video_id}"
        asset.youtube_video_asset.youtube_video_id = video_id
        operations.append((operation, f"YouTube video asset '{video_id}'"))

    results = mutations.mutate(
        client, customer_id, operations, validate_only=validate_only
    )
    if validate_only:
        return [
            {"youtube_video_id": v, "validate_only": "valid"} for v in video_ids
        ]
    return [
        {
            "youtube_video_id": video_id,
            "asset_id": mutations.parse_id(rn),
            "resource_name": rn,
        }
        for video_id, rn in zip(video_ids, results.get("asset", []))
    ]


def _youtube_video_id(value: str) -> str:
    """Extracts the video ID from a YouTube URL, or returns the value as is."""
    value = value.strip()
    parsed = urllib.parse.urlparse(value)
    if parsed.hostname and "youtu.be" in parsed.hostname:
        return parsed.path.lstrip("/")
    if parsed.hostname and "youtube" in parsed.hostname:
        query_id = urllib.parse.parse_qs(parsed.query).get("v")
        if query_id:
            return query_id[0]
        return parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return value


def _create_text_assets(
    client, customer_id: str, texts: List[str], validate_only: bool
) -> List[str]:
    """Creates text assets in their own request and returns resource names."""
    operations = []
    for text in texts:
        operation = client.get_type("MutateOperation")
        operation.asset_operation.create.text_asset.text = text
        operations.append((operation, f"text asset '{text}'"))
    results = mutations.mutate(
        client, customer_id, operations, validate_only=validate_only
    )
    return results.get("asset", [])


def build_asset_group_operations(
    client,
    customer_id: str,
    ids: mutations.TempIds,
    campaign_rn: str,
    name: str,
    final_url: str,
    headlines: List[str],
    long_headlines: List[str],
    descriptions: List[str],
    business_name: str | None,
    image_asset_ids: Dict[str, List[str | int]],
    audience_ids: List[str | int] | None,
    search_themes: List[str] | None,
    brand_guidelines_enabled: bool,
    validate_only: bool,
) -> List[Tuple[Any, str]]:
    """Builds the operations that create a Performance Max asset group.

    Headlines and descriptions must exist before the asset group is created,
    so unless validate_only is set they are created in a separate request
    first. Text assets are free and deduplicated, so this leaves nothing
    behind that matters if the main request then fails.

    Args:
        image_asset_ids: Map of asset field type name (e.g. "MARKETING_IMAGE")
          to IDs of existing image assets.
        brand_guidelines_enabled: When true, the business name and logos are
          linked to the campaign rather than to the asset group.

    Returns:
        (MutateOperation, label) pairs, to be sent in the same request as the
        campaign when the campaign is new.
    """
    field_types = client.enums.AssetFieldTypeEnum
    operations: List[Tuple[Any, str]] = []

    asset_group_rn = ids.resource_name(customer_id, "assetGroups")
    operation = client.get_type("MutateOperation")
    asset_group = operation.asset_group_operation.create
    asset_group.resource_name = asset_group_rn
    asset_group.name = name
    asset_group.campaign = campaign_rn
    asset_group.final_urls.append(final_url)
    operations.append((operation, f"asset group '{name}'"))

    def link(asset_rn: str, field_type_name: str, label: str):
        field_type = field_types[field_type_name]
        op = client.get_type("MutateOperation")
        if brand_guidelines_enabled and field_type_name in (
            "BUSINESS_NAME",
            "LOGO",
            "LANDSCAPE_LOGO",
        ):
            link_asset = op.campaign_asset_operation.create
            link_asset.campaign = campaign_rn
        else:
            link_asset = op.asset_group_asset_operation.create
            link_asset.asset_group = asset_group_rn
        link_asset.asset = asset_rn
        link_asset.field_type = field_type
        operations.append((op, f"{field_type_name} {label}"))

    def inline_text_asset(text: str) -> str:
        """Creates a text asset in the same request, with a temporary ID."""
        asset_rn = ids.resource_name(customer_id, "assets")
        op = client.get_type("MutateOperation")
        op.asset_operation.create.resource_name = asset_rn
        op.asset_operation.create.text_asset.text = text
        operations.insert(0, (op, f"text asset '{text}'"))
        return asset_rn

    repeated_texts = [("HEADLINE", t) for t in headlines] + [
        ("DESCRIPTION", t) for t in descriptions
    ]
    if validate_only:
        repeated_rns = [inline_text_asset(t) for _, t in repeated_texts]
    else:
        repeated_rns = _create_text_assets(
            client, customer_id, [t for _, t in repeated_texts], False
        )
    for (field_type_name, text), asset_rn in zip(repeated_texts, repeated_rns):
        link(asset_rn, field_type_name, f"'{text}'")

    for text in long_headlines:
        link(inline_text_asset(text), "LONG_HEADLINE", f"'{text}'")
    if business_name:
        link(
            inline_text_asset(business_name),
            "BUSINESS_NAME",
            f"'{business_name}'",
        )

    for field_type_name, asset_ids in image_asset_ids.items():
        for asset_id in asset_ids or []:
            link(
                mutations.resource_name(customer_id, "assets", asset_id),
                field_type_name,
                f"asset {asset_id}",
            )

    for audience_id in audience_ids or []:
        op = client.get_type("MutateOperation")
        signal = op.asset_group_signal_operation.create
        signal.asset_group = asset_group_rn
        signal.audience.audience = mutations.resource_name(
            customer_id, "audiences", audience_id
        )
        operations.append((op, f"audience signal {audience_id}"))
    for theme in search_themes or []:
        op = client.get_type("MutateOperation")
        signal = op.asset_group_signal_operation.create
        signal.asset_group = asset_group_rn
        signal.search_theme.text = theme
        operations.append((op, f"search theme signal '{theme}'"))

    return operations


def check_asset_group_inputs(
    headlines: List[str],
    long_headlines: List[str],
    descriptions: List[str],
    business_name: str | None,
    require_brand_assets: bool,
    marketing_image_asset_ids: List[str | int] | None,
    square_marketing_image_asset_ids: List[str | int] | None,
    logo_asset_ids: List[str | int] | None,
) -> Tuple[List[str], List[str], List[str]]:
    """Checks the minimum Performance Max asset requirements."""
    headlines = mutations.check_texts("headlines", headlines, 3, 15, 30)
    long_headlines = mutations.check_texts(
        "long_headlines", long_headlines, 1, 5, 90
    )
    descriptions = mutations.check_texts("descriptions", descriptions, 2, 5, 90)
    if not any(len(d) <= 60 for d in descriptions):
        raise ToolError(
            "descriptions: at least one description must be 60 characters or fewer."
        )
    if business_name and len(business_name) > 25:
        raise ToolError("business_name must be at most 25 characters.")
    if not marketing_image_asset_ids:
        raise ToolError("At least one marketing (1.91:1) image is required.")
    if not square_marketing_image_asset_ids:
        raise ToolError(
            "At least one square marketing (1:1) image is required."
        )
    if require_brand_assets and not business_name:
        raise ToolError("business_name is required.")
    if require_brand_assets and not logo_asset_ids:
        raise ToolError("At least one logo (1:1) image is required.")
    return headlines, long_headlines, descriptions


@assets_mcp.tool(annotations=_CREATE)
def create_asset_group(
    customer_id: str | int,
    campaign_id: str | int,
    name: str,
    final_url: str,
    headlines: List[str],
    long_headlines: List[str],
    descriptions: List[str],
    marketing_image_asset_ids: List[str | int],
    square_marketing_image_asset_ids: List[str | int],
    business_name: str | None = None,
    logo_asset_ids: List[str | int] | None = None,
    portrait_marketing_image_asset_ids: List[str | int] | None = None,
    landscape_logo_asset_ids: List[str | int] | None = None,
    video_asset_ids: List[str | int] | None = None,
    audience_ids: List[str | int] | None = None,
    search_themes: List[str] | None = None,
    validate_only: bool = False,
    login_customer_id: str | int | None = None,
) -> Dict[str, Any]:
    """Adds an asset group to an existing Performance Max campaign.

    An asset group is a set of creatives (text, images, logos, videos) plus a
    final URL and optional audience signals, which Google combines into ads.
    Upload images with upload_image / upload_logo first and pass their asset IDs.

    Requirements:
      - 3-15 headlines (max 30 characters each)
      - 1-5 long headlines (max 90 characters each)
      - 2-5 descriptions (max 90 characters, at least one of max 60)
      - 1+ landscape (1.91:1) and 1+ square (1:1) marketing images
      - business_name (max 25 characters) and 1+ square logo, unless the
        campaign uses brand guidelines, where these are set on the campaign.
      - Videos are optional; Google may generate videos if none are provided.

    Args:
        customer_id: The Google Ads customer ID.
        campaign_id: The ID of a Performance Max campaign.
        name: The asset group name. Must be unique within the campaign.
        final_url: The landing page URL.
        headlines: Short headlines.
        long_headlines: Long headlines.
        descriptions: Descriptions.
        marketing_image_asset_ids: IDs of landscape (1.91:1) image assets.
        square_marketing_image_asset_ids: IDs of square (1:1) image assets.
        business_name: The advertiser or brand name.
        logo_asset_ids: IDs of square (1:1) logo image assets.
        portrait_marketing_image_asset_ids: IDs of portrait (4:5) image assets.
        landscape_logo_asset_ids: IDs of landscape (4:1) logo image assets.
        video_asset_ids: IDs of YouTube video assets (see create_youtube_video_assets).
        audience_ids: IDs of audiences to use as audience signals.
        search_themes: Search themes to use as signals, e.g. "budget tracking app".
        validate_only: If true, validates the request without creating anything.
        login_customer_id: Optional manager customer ID to use as the login-customer-id header.

    Returns:
        The resource names of the created entities.
    """
    customer_id = utils.clean_customer_id(customer_id)
    campaign_id = mutations.parse_id(campaign_id, "campaign_id")
    client = utils.get_googleads_client(login_customer_id=login_customer_id)

    rows = mutations.search(
        client,
        customer_id,
        "SELECT campaign.advertising_channel_type, "
        "campaign.brand_guidelines_enabled FROM campaign "
        f"WHERE campaign.id = {campaign_id}",
    )
    if not rows:
        raise ToolError(f"Campaign {campaign_id} not found.")
    if rows[0]["campaign.advertising_channel_type"] != "PERFORMANCE_MAX":
        raise ToolError(
            f"Campaign {campaign_id} is not a Performance Max campaign."
        )
    brand_guidelines_enabled = bool(
        rows[0]["campaign.brand_guidelines_enabled"]
    )

    headlines, long_headlines, descriptions = check_asset_group_inputs(
        headlines,
        long_headlines,
        descriptions,
        business_name,
        not brand_guidelines_enabled,
        marketing_image_asset_ids,
        square_marketing_image_asset_ids,
        logo_asset_ids,
    )
    if brand_guidelines_enabled and (
        business_name or logo_asset_ids or landscape_logo_asset_ids
    ):
        raise ToolError(
            "This campaign uses brand guidelines: its business name and logos "
            "are set on the campaign. Omit business_name and logo asset IDs."
        )

    operations = build_asset_group_operations(
        client,
        customer_id,
        mutations.TempIds(),
        mutations.resource_name(customer_id, "campaigns", campaign_id),
        name,
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
    if validate_only:
        return {"validate_only": True, "message": "Request is valid."}
    asset_group_rn = results["asset_group"][0]
    return {
        "asset_group_id": mutations.parse_id(asset_group_rn),
        "created": results,
    }
