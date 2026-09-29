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

"""Tests for the asset tools."""

import base64
import os
import unittest
from unittest.mock import patch

from fastmcp.exceptions import ToolError

from ads_mcp.tools import assets
from tests.tools.ads_fakes import MutateToolTestCase

_PNG = b"\x89PNG\r\n\x1a\nfake"


class TestImageHelpers(unittest.TestCase):

    def test_suitable_image_field_types(self):
        self.assertEqual(
            assets.suitable_image_field_types(1200, 628), ["MARKETING_IMAGE"]
        )
        self.assertEqual(
            assets.suitable_image_field_types(1200, 1200),
            ["SQUARE_MARKETING_IMAGE", "LOGO"],
        )
        self.assertEqual(assets.suitable_image_field_types(200, 200), ["LOGO"])
        self.assertEqual(
            assets.suitable_image_field_types(1200, 300), ["LANDSCAPE_LOGO"]
        )
        self.assertEqual(assets.suitable_image_field_types(0, 0), [])

    def test_load_image_requires_exactly_one_source(self):
        with self.assertRaisesRegex(ToolError, "exactly one"):
            assets._load_image(None, None, None)
        with self.assertRaisesRegex(ToolError, "exactly one"):
            assets._load_image("https://example.com/a.png", "abc", None)

    def test_load_image_from_base64_data_url(self):
        data_url = "data:image/png;base64," + base64.b64encode(_PNG).decode()
        self.assertEqual(assets._load_image(None, data_url, None)[0], _PNG)

    @patch.dict(
        os.environ,
        {
            "GOOGLE_ADS_MCP_OAUTH_CLIENT_ID": "id",
            "GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET": "secret",
        },
    )
    def test_file_path_disabled_when_hosted(self):
        with self.assertRaisesRegex(ToolError, "web service"):
            assets._load_image(None, None, "/etc/passwd")

    def test_refuses_private_addresses(self):
        for url in [
            "http://127.0.0.1/logo.png",
            "http://169.254.169.254/latest/meta-data",
            "http://10.0.0.1/x.png",
        ]:
            with self.assertRaisesRegex(ToolError, "non-public"):
                assets._check_public_host(url)
        with self.assertRaisesRegex(ToolError, "http"):
            assets._check_public_host("file:///etc/passwd")

    def test_youtube_video_id(self):
        for value in [
            "dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        ]:
            self.assertEqual(assets._youtube_video_id(value), "dQw4w9WgXcQ")


class TestUploadImage(MutateToolTestCase):

    def test_upload_reports_dimensions_and_fit(self):
        self.search_results = [
            [
                {
                    "asset.name": "logo",
                    "asset.image_asset.full_size.width_pixels": 1200,
                    "asset.image_asset.full_size.height_pixels": 628,
                    "asset.image_asset.mime_type": "IMAGE_PNG",
                }
            ]
        ]
        result = assets.upload_logo(
            "1", image_base64=base64.b64encode(_PNG).decode(), name="logo"
        )

        [[operation]] = self.mutate_calls()
        asset = operation.asset_operation.create
        self.assertEqual(asset.type_.name, "IMAGE")
        self.assertEqual(asset.image_asset.data, _PNG)
        self.assertEqual(result["asset_id"], "1000")
        self.assertEqual(result["suitable_field_types"], ["MARKETING_IMAGE"])
        self.assertIn("does not fit", result["warning"])


class TestCreateAssetGroup(MutateToolTestCase):

    def _create(self, **kwargs):
        args = dict(
            customer_id="1",
            campaign_id="5",
            name="Group 2",
            final_url="https://example.com/app",
            headlines=["One", "Two", "Three"],
            long_headlines=["A long headline"],
            descriptions=["Short one.", "Another description."],
            marketing_image_asset_ids=[11],
            square_marketing_image_asset_ids=[12],
            business_name="Budgetly",
            logo_asset_ids=[13],
        )
        args.update(kwargs)
        return assets.create_asset_group(**args)

    def test_adds_asset_group_to_pmax_campaign(self):
        self.search_results = [
            [
                {
                    "campaign.advertising_channel_type": "PERFORMANCE_MAX",
                    "campaign.brand_guidelines_enabled": False,
                }
            ]
        ]
        result = self._create()

        _, operations = self.mutate_calls()
        [asset_group] = self.created(operations, "asset_group_operation")
        self.assertEqual(asset_group.campaign, "customers/1/campaigns/5")
        self.assertIn("asset_group_id", result)

    def test_rejects_non_pmax_campaign(self):
        self.search_results = [
            [
                {
                    "campaign.advertising_channel_type": "SEARCH",
                    "campaign.brand_guidelines_enabled": False,
                }
            ]
        ]
        with self.assertRaisesRegex(ToolError, "not a Performance Max"):
            self._create()

    def test_brand_guidelines_campaign_rejects_brand_assets(self):
        self.search_results = [
            [
                {
                    "campaign.advertising_channel_type": "PERFORMANCE_MAX",
                    "campaign.brand_guidelines_enabled": True,
                }
            ]
        ]
        with self.assertRaisesRegex(ToolError, "brand guidelines"):
            self._create()


class TestAssetReads(MutateToolTestCase):

    def test_list_assets(self):
        self.search_results = [
            [
                {
                    "asset.id": 1,
                    "asset.type": "IMAGE",
                    "asset.image_asset.full_size.width_pixels": 1200,
                    "asset.image_asset.full_size.height_pixels": 1200,
                },
                {
                    "asset.id": 2,
                    "asset.type": "TEXT",
                    "asset.text_asset.text": "Budget smarter",
                },
            ]
        ]
        result = assets.list_assets("1", asset_types=["IMAGE", "TEXT"])

        self.assertEqual(
            result[0]["suitable_field_types"],
            ["SQUARE_MARKETING_IMAGE", "LOGO"],
        )
        self.assertEqual(result[1]["text"], "Budget smarter")
        self.assertIn("asset.type IN ('IMAGE', 'TEXT')", self.queries[0])

    def test_get_asset_group_groups_assets_by_field_type(self):
        self.search_results = [
            [{"asset_group.id": 8, "asset_group.ad_strength": "GOOD"}],
            [
                {
                    "asset_group_asset.field_type": "HEADLINE",
                    "asset.id": 1,
                    "asset.text_asset.text": "One",
                },
                {
                    "asset_group_asset.field_type": "LOGO",
                    "asset.id": 2,
                    "asset.name": "logo.png",
                },
            ],
            [{"asset_group_signal.search_theme.text": "budget app"}],
        ]
        result = assets.get_asset_group("1", 8)

        self.assertEqual(result["ad_strength"], "GOOD")
        self.assertEqual(result["assets"]["HEADLINE"][0]["text"], "One")
        self.assertEqual(result["assets"]["LOGO"][0]["name"], "logo.png")
        self.assertEqual(
            result["signals"], [{"search_theme.text": "budget app"}]
        )


def _link(asset_id, field_type, campaign_id=5, **asset_fields):
    return {
        "campaign_asset.resource_name": (
            f"customers/1/campaignAssets/{campaign_id}~{asset_id}~{field_type}"
        ),
        "campaign_asset.field_type": field_type,
        "campaign.id": campaign_id,
        "asset.id": asset_id,
        "asset.name": "",
        **asset_fields,
    }


class TestRemoveCampaignAssets(MutateToolTestCase):

    def _unlinked(self):
        [operations] = self.mutate_calls()
        # Links are removed; assets are never touched.
        self.assertTrue(
            all(
                op._pb.WhichOneof("operation") == "campaign_asset_operation"
                and op.campaign_asset_operation._pb.WhichOneof("operation")
                == "remove"
                for op in operations
            )
        )
        return [op.campaign_asset_operation.remove for op in operations]

    def test_unlinks_by_asset_id_and_field_type(self):
        self.search_results = [
            [
                _link(
                    9,
                    "CALLOUT",
                    **{"asset.callout_asset.callout_text": "Free shipping"},
                )
            ]
        ]
        result = assets.remove_campaign_assets(
            "1", campaign_id=5, asset_ids=[9], field_type="CALLOUT"
        )
        self.assertEqual(
            self._unlinked(), ["customers/1/campaignAssets/5~9~CALLOUT"]
        )
        for condition in [
            "campaign.id = 5",
            "asset.id IN (9)",
            "campaign_asset.field_type = 'CALLOUT'",
            "campaign_asset.status != 'REMOVED'",
        ]:
            self.assertIn(condition, self.queries[0])
        self.assertEqual(
            result["unlinked"],
            [
                {
                    "campaign_id": "5",
                    "asset_id": "9",
                    "field_type": "CALLOUT",
                    "text": "Free shipping",
                }
            ],
        )

    def test_without_field_type_unlinks_every_link_of_the_asset(self):
        self.search_results = [
            [_link(13, "BUSINESS_LOGO"), _link(13, "AD_IMAGE")]
        ]
        result = assets.remove_campaign_assets(
            "1", campaign_id=5, asset_ids=["13"]
        )
        self.assertEqual(
            self._unlinked(),
            [
                "customers/1/campaignAssets/5~13~BUSINESS_LOGO",
                "customers/1/campaignAssets/5~13~AD_IMAGE",
            ],
        )
        self.assertNotIn("field_type =", self.queries[0])
        self.assertEqual(len(result["unlinked"]), 2)

    def test_unlinks_by_resource_name(self):
        rn = "customers/1/campaignAssets/6~9~SITELINK"
        self.search_results = [
            [
                _link(
                    9,
                    "SITELINK",
                    campaign_id=6,
                    **{"asset.sitelink_asset.link_text": "Pricing"},
                )
            ]
        ]
        result = assets.remove_campaign_assets("1", resource_names=[rn])
        self.assertEqual(self._unlinked(), [rn])
        self.assertIn(
            f"campaign_asset.resource_name IN ('{rn}')", self.queries[0]
        )
        self.assertEqual(result["unlinked"][0]["text"], "Pricing")

    def test_missing_link_changes_nothing(self):
        self.search_results = [[_link(9, "CALLOUT")]]
        with self.assertRaisesRegex(ToolError, "asset 10"):
            assets.remove_campaign_assets("1", campaign_id=5, asset_ids=[9, 10])
        self.service.mutate.assert_not_called()

    def test_rejects_invalid_input(self):
        for kwargs in [
            {},
            {"asset_ids": [9]},
            {"campaign_id": 5, "field_type": "CALLOUT"},
            {"campaign_id": 5, "asset_ids": [9], "field_type": "HEADLINE"},
            {"campaign_id": 5, "asset_ids": ["9 OR 1=1"]},
            {"resource_names": ["customers/2/campaignAssets/5~9~CALLOUT"]},
            {"resource_names": ["customers/1/campaignAssets/5~9"]},
            {"resource_names": ["5~9~CALLOUT' OR 'a'='a"]},
        ]:
            with self.assertRaises(ToolError, msg=kwargs):
                assets.remove_campaign_assets("1", **kwargs)
        self.assertEqual(self.queries, [])

    def test_validate_only(self):
        self.search_results = [[_link(9, "CALLOUT")]]
        result = assets.remove_campaign_assets(
            "1", campaign_id=5, asset_ids=[9], validate_only=True
        )
        self.assertTrue(
            self.service.mutate.call_args.kwargs["request"].validate_only
        )
        self.assertTrue(result["validate_only"])


if __name__ == "__main__":
    unittest.main()
