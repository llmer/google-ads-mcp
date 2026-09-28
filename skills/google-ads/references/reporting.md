# Reporting with `search`

`search(customer_id, fields, resource, conditions, orderings, limit)` builds a
GAQL query. Use full field names (`campaign.name`, not `name`), check
unfamiliar fields with `get_resource_metadata(resource_name)`, and give dates
as `YYYY-MM-DD` or a `DURING` range (`TODAY`, `LAST_7_DAYS`, `LAST_30_DAYS`,
`THIS_MONTH`, `LAST_MONTH`). Cost fields are in micros: divide by 1,000,000.

## Spend per day

- resource: `campaign`
- fields: `segments.date`, `campaign.name`, `metrics.cost_micros`, `metrics.conversions`
- conditions: `segments.date DURING LAST_14_DAYS`
- orderings: `segments.date`

## Search terms (find negative keywords)

- resource: `search_term_view`
- fields: `search_term_view.search_term`, `campaign.name`, `metrics.clicks`,
  `metrics.cost_micros`, `metrics.conversions`
- conditions: `segments.date DURING LAST_30_DAYS`, `metrics.clicks > 0`
- orderings: `metrics.cost_micros DESC`

Terms with spend and no conversions are candidates for `set_negative_keywords`.

## Conversions by action

- resource: `campaign`
- fields: `campaign.name`, `segments.conversion_action_name`,
  `metrics.all_conversions`, `metrics.all_conversions_value`
- conditions: `segments.date DURING LAST_30_DAYS`

## Performance Max asset status

- resource: `asset_group_asset`
- fields: `asset_group.name`, `asset_group_asset.field_type`, `asset.id`,
  `asset.text_asset.text`, `asset_group_asset.primary_status`,
  `asset_group_asset.primary_status_reasons`
- conditions: `asset_group_asset.status = 'ENABLED'`

For per-asset metrics, check which `metrics.*` fields
`get_resource_metadata("asset_group_asset")` lists as selectable.

## Why isn't a campaign spending?

Start with `get_campaign`: `primary_status` and `primary_status_reasons`
explain most cases (paused, limited by budget, pending review, disapproved
ads, bid strategy learning). Then check ad review status:

- resource: `ad_group_ad`
- fields: `ad_group_ad.ad.id`, `ad_group_ad.policy_summary.approval_status`,
  `ad_group_ad.policy_summary.policy_topic_entries`
- conditions: `campaign.id = <id>`

## Keywords and ads of a Search campaign

IDs for `set_keyword_status` and `set_ad_status`:

- resource: `ad_group_criterion`
- fields: `ad_group.id`, `ad_group_criterion.criterion_id`,
  `ad_group_criterion.keyword.text`, `ad_group_criterion.keyword.match_type`,
  `ad_group_criterion.status`, `metrics.clicks`, `metrics.cost_micros`
- conditions: `campaign.id = <id>`, `ad_group_criterion.type = 'KEYWORD'`,
  `ad_group_criterion.negative = FALSE`,
  `ad_group_criterion.status != 'REMOVED'`, `segments.date DURING LAST_30_DAYS`

- resource: `ad_group_ad`
- fields: `ad_group_ad.resource_name`, `ad_group_ad.ad.id`,
  `ad_group_ad.status`, `ad_group_ad.policy_summary.approval_status`,
  `ad_group_ad.ad.responsive_search_ad.headlines`
- conditions: `campaign.id = <id>`, `ad_group_ad.status != 'REMOVED'`

Campaign assets for `remove_campaign_assets`:

- resource: `campaign_asset`
- fields: `campaign_asset.resource_name`, `campaign_asset.field_type`,
  `asset.id`, `asset.sitelink_asset.link_text`,
  `asset.callout_asset.callout_text`
- conditions: `campaign.id = <id>`, `campaign_asset.status != 'REMOVED'`
