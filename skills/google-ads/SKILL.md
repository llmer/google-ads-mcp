---
name: google-ads
description: How to use the Google Ads MCP server to inspect, create and manage Google Ads campaigns safely, including Search, Performance Max and App campaigns, images and text assets, targeting, budgets and conversion tracking. Use this skill whenever the user wants to advertise a website, web app or mobile app on Google, launch or change a campaign, check ad spend or campaign performance, adjust budgets, upload ad creatives, set up conversion tracking or report conversions, even if they don't say "Google Ads" explicitly (e.g. "get more installs for my iOS app", "why is my PMax campaign not spending", "pause everything in account 123-456-7890").
---

# Google Ads with the Google Ads MCP server

This server can read a Google Ads account and change it. Changes can spend
real money, so the core habit is: **read the current state, show the user a
plan, dry-run it, apply it, then read it back.**

Tool names below are the base names. The client usually adds a namespace
prefix, e.g. `campaigns_create_pmax_campaign`, and your client may add a
server prefix too (`mcp__google-ads__...`).

## Tool map

| Need | Tools |
|---|---|
| Find accounts | `list_accounts` (use `list_accessible_customers` only for raw IDs) |
| What is running and spending | `get_spend_overview`, `list_campaigns`, `get_campaign` |
| Anything else, custom reports | `search` (GAQL), `get_resource_metadata` for field names |
| Create campaigns | `create_search_campaign`, `create_pmax_campaign`, `create_app_campaign` |
| Change campaigns | `update_budget`, `pause_campaign`, `enable_campaign`, `update_campaign_settings`, `set_bidding_strategy`, `set_cpc_bids`, `set_auto_apply_recommendations` |
| Edit Search ad groups, ads and keywords | `update_ad_group`, `create_responsive_search_ad`, `set_ad_status`, `add_keywords`, `set_keyword_status` |
| Creatives | `list_assets`, `upload_image`, `upload_logo`, `create_text_assets`, `create_youtube_video_assets`, `create_asset_group`, `get_asset_group`, `add_campaign_assets`, `remove_campaign_assets` |
| Targeting | `find_geo_targets`, `list_audiences`, `set_geo_targets`, `set_language_targets`, `set_negative_keywords`, `set_audience_signals`, `list_account_exclusions`, `set_account_exclusions` (account-wide; the only placement and content exclusions App campaigns honour) |
| Conversions | `list_conversion_actions`, `get_conversion_action`, `create_conversion_action`, `update_conversion_action`, `upload_click_conversions`, `set_campaign_conversion_goals` |

## The workflow for any change

1. **Orient.** Call `list_accounts` to pick the account, note its
   `currency_code`, and if the account was reached through a manager account,
   pass the returned `login_customer_id` on every call. Then call
   `get_spend_overview` and `list_campaigns` so you know what already runs,
   what it costs, and how much headroom the guardrails leave. Skipping this is
   how duplicate campaigns and surprise spend happen.
2. **Check prerequisites.** Smart bidding (the default for new campaigns)
   optimizes towards conversions, so a campaign without working conversion
   tracking will spend without learning. Check `list_conversion_actions` and,
   for a key action, `get_conversion_action` (it shows conversions in the last
   30 days). If there is nothing, set tracking up first; see
   [references/conversions.md](references/conversions.md).
3. **Plan and confirm.** Tell the user, in plain terms: campaign type, name,
   daily budget *in the account currency*, locations, languages, bidding and
   goal, and the creatives. Mention that Google may spend up to 2x the daily
   budget on a given day (never more than 30.4x per month). Get a yes before
   creating anything that could later spend.
4. **Dry-run.** Every mutating tool accepts `validate_only=true`. Use it for
   anything non-trivial; the API then reports problems (policy, missing
   assets, bad IDs) without changing the account.
5. **Apply.** Campaigns are always created **paused**, so creation alone
   never spends.
6. **Verify.** Read back with `get_campaign` (and `get_asset_group` for
   Performance Max) and summarize what now exists, with IDs.
7. **Enable only on explicit request.** `enable_campaign` starts spending.
   Restate the campaign, its daily budget and the new account total from the
   dry-run before calling it.

For read-only questions ("how is my campaign doing?") only step 1 applies:
read, then answer. Don't offer to change things the user didn't ask about.

## Choosing a campaign type

- **Mobile app installs or in-app actions** → `create_app_campaign`. This is
  the only type that promotes app store installs; Performance Max cannot.
  Goal `INSTALLS` needs nothing else. `IN_APP_ACTIONS` / `IN_APP_VALUE` need
  in-app conversion actions from Firebase, GA4 or an app analytics partner.
- **Website or web app, people searching for what you offer** →
  `create_search_campaign`. Most control, works with modest budgets, keywords
  make intent explicit. A good first campaign.
- **Website or web app, reach across all Google channels** →
  `create_pmax_campaign`. Needs working conversion tracking and a full set of
  creatives (text, landscape + square images, logo). Best once conversions
  flow; avoid it as the very first campaign on an account with no data.

When unsure, ask what the user wants more of (installs, sign-ups, purchases)
and where those happen (app store vs. website).

App campaign `app_id`: the package name for Android (`com.example.app`), the
numeric ID from the App Store URL for iOS (`apps.apple.com/.../id123456789` →
`123456789`). Android and iOS need separate campaigns.

## Money and guardrails

- Amounts are in the account currency as plain numbers: `25.5` means 25.50
  in that currency. Never convert to micros; the tools do that.
- `update_budget` refuses shared budgets unless `allow_shared_budget=true`;
  set it only after telling the user which other campaigns are affected.
- The server enforces spend guardrails from its config (max budget per
  campaign, max total daily budget of enabled campaigns, max increase per
  change, max CPC bid or bid ceiling). When a call is refused with
  "Guardrail: ...", report the limit and the numbers to the user and stop.
  Don't work around it by splitting a change into smaller calls, pausing
  unrelated campaigns, or creating new
  campaigns instead; the limit exists to stop exactly that. Stepwise increases
  are fine only when the user explicitly asks for them, one step per decision.
- `get_spend_overview` → `guardrails.daily_budget_headroom` shows how much
  daily budget can still be enabled.

## Editing existing Search campaigns

- Read first: `get_campaign` shows the bidding strategy and lists the ad
  groups. Find keyword criterion IDs and ad IDs with `search`; see
  [references/reporting.md](references/reporting.md).
- Tools that take `ad_group_id` also accept `campaign_id` for a campaign with
  exactly one ad group (as `create_search_campaign` makes).
- Nothing here removes ads, keywords or ad groups; pause them instead.
  `remove_campaign_assets` only unlinks assets from the campaign.
- New keywords and ads are enabled by default and start spending at once in
  an enabled campaign. Confirm them with the user, or create the ad with
  `status="PAUSED"` to stage it.
- `set_bidding_strategy` replaces the whole strategy (targets not given are
  cleared) and restarts Smart Bidding's learning. State the current strategy
  (the result's `previous`) and the new one, and get a yes first.
- Ad policy problems come back with their policy topics (e.g.
  `DESTINATION_NOT_WORKING`); fix the copy or URL instead of retrying.
- A new end date for an enabled campaign that has ended is refused, since it
  would restart spending: pause it, change the date, then `enable_campaign`.

## Targeting

- Locations: `find_geo_targets(["Austin", "Texas"], country_code="US")` returns
  IDs (2840 is the United States). Without location targeting a campaign
  targets every country; the create tools warn about this. Always set it.
- Languages: 1000 English, 1003 Spanish, 1002 French, 1001 German,
  1005 Japanese.
- `set_*` tools take `mode`: `add` (default, keeps existing), `remove`, or
  `replace` (makes the list exact, removing anything else). Use `replace` only
  when the user gave the complete list, and dry-run it first: the result shows
  what would be removed.
- Keyword syntax (keywords and negatives): `[exact phrase]`, `"phrase match"`,
  bare text = broad for negatives / `keyword_match_type` for keywords.
- Negative keywords are cheap insurance for app and SaaS advertisers: add
  terms like `free` (if paid), `jobs`, `careers`, `login`, `download apk`, and
  competitor or support queries the user doesn't want to pay for.
- Performance Max audience signals (`set_audience_signals`) are hints, not
  restrictions. Use `list_audiences` for IDs, or search themes (short phrases).

## Creatives

Check `list_assets` before uploading; reuse what exists. `upload_image` and
`upload_logo` report `suitable_field_types`, which tells you where each image
fits (landscape `MARKETING_IMAGE`, `SQUARE_MARKETING_IMAGE`, `LOGO`...). Pass
asset IDs to the create tools, choosing images by their suitable field types.
Text limits and full requirements per campaign type:
[references/asset-specs.md](references/asset-specs.md). Write copy that
fits the limits the first time rather than relying on the API to reject it.

## Errors

Errors name the failing operation, e.g.
`... (field_error.REQUIRED) at mutate_operations[4].asset_group_asset [operation: LOGO asset 13]`.
Fix that input and retry with `validate_only=true`. Policy disapprovals
(trademarks, misleading claims) need different copy, not retries. Permission
errors on client accounts usually mean a missing `login_customer_id`.

## Reporting

For anything the dedicated tools don't cover, use `search`. Look up valid
fields with `get_resource_metadata` first rather than guessing; see
[references/reporting.md](references/reporting.md) for common queries
(search terms, per-day spend, asset performance, conversions by action).
