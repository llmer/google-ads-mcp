# Creative requirements by campaign type

Character limits count wide characters (e.g. CJK) as two.

## Search: `create_search_campaign`, `create_responsive_search_ad`

| Field | Count | Max length |
|---|---|---|
| headlines | 3–15 (aim for 10+) | 30 |
| descriptions | 2–4 (aim for 4) | 90 |
| path1, path2 | optional (path2 needs path1) | 15 each |

Responsive search ads mix headlines, so each must stand alone. Vary them:
product name, key benefit, offer, call to action, a keyword-rich one. Avoid
repeating the same phrase across several headlines.

`create_responsive_search_ad` can pin a headline to `HEADLINE_1`-`3` or a
description to `DESCRIPTION_1`-`2` (`{"text": ..., "pin": "HEADLINE_1"}`),
e.g. to keep a required disclaimer or the brand name in place. Pin as little
as possible: every pin lowers ad strength and the combinations Google tests.

## Performance Max: `create_pmax_campaign`, `create_asset_group`

| Field | Count | Spec |
|---|---|---|
| headlines | 3–15 | max 30 characters |
| long_headlines | 1–5 | max 90 characters |
| descriptions | 2–5 | max 90, at least one max 60 |
| business_name | 1 | max 25 characters |
| marketing_image_asset_ids | 1–20 (aim for 3+) | landscape 1.91:1, min 600x314, recommended 1200x628 |
| square_marketing_image_asset_ids | 1–20 (aim for 3+) | 1:1, min 300x300, recommended 1200x1200 |
| portrait_marketing_image_asset_ids | optional | 4:5, min 480x600, recommended 960x1200 |
| logo_asset_ids | 1–5 | 1:1, min 128x128, recommended 1200x1200 |
| landscape_logo_asset_ids | optional | 4:1, min 512x128, recommended 1200x300 |
| video_asset_ids | optional, up to 5 | YouTube; Google may generate videos when none are given |

With `brand_guidelines_enabled=true` the business name and logos belong to the
campaign, and `create_asset_group` for that campaign must omit them.

Ad strength (from `get_asset_group`) improves with more variety: more
headlines, several images of each shape, and a video.

## App: `create_app_campaign`

| Field | Count | Spec |
|---|---|---|
| headlines | 1–5 | max 30 characters |
| descriptions | 1–5 | max 90 characters |
| image_asset_ids | optional, up to 20 | landscape 1.91:1, square 1:1 or portrait 4:5 |
| video_asset_ids | optional, up to 20 | YouTube; landscape, square and portrait |

App campaigns also pull the app's store listing (icon, screenshots, rating),
so text-only campaigns can serve, but images and videos broaden reach.

## Image files

- JPEG, PNG or GIF, at most 5120 KB.
- Keep text overlay under ~20% of the image, and keep the logo out of
  marketing images (logos go in the logo fields).
- `upload_image` accepts `image_url` (public http/https), `image_base64`, or
  `file_path` (only when the server runs locally).
