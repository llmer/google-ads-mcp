# Conversion tracking

Bidding can only optimize for what it can see. Set tracking up before
launching smart-bidding campaigns, and confirm it records data
(`get_conversion_action` → `last_30_days`).

## Websites and web apps

**Option A: Google tag (`conversion_type="WEBPAGE"`)**, when the conversion
happens in the browser (sign-up confirmation page, checkout success).

1. `create_conversion_action(name="Sign-up", category="SIGNUP")`.
2. The result's `tag_snippets` contain the global site tag (install on every
   page) and the event snippet (fire on the conversion). Give both to the user
   with where each goes; for single-page apps the event snippet is called in
   code when the conversion happens, not on page load.
3. After deploying, check `get_conversion_action` in a day or two.

**Option B: server-side upload (`conversion_type="UPLOAD_CLICKS"`)**, when the
conversion is confirmed on the backend (paid subscription, verified account),
or ad blockers make browser tags unreliable.

1. `create_conversion_action(name="Paid subscription", category="SUBSCRIBE_PAID", conversion_type="UPLOAD_CLICKS")`.
2. The web app must capture the `gclid` URL parameter (or `gbraid`/`wbraid`
   for iOS traffic) on landing, store it with the user, and later report:
   `upload_click_conversions(conversions=[{conversion_action_id, gclid,
   conversion_date_time: "2026-09-27 14:05:00-07:00", conversion_value,
   currency_code, order_id}])`.
3. Uploads work a few hours after the action is created, for clicks up to 90
   days old. The result lists per-conversion errors; the rest succeed.
4. For users in the EEA, pass `ad_user_data_consent`.

Use `order_id` so retries don't double count.

Categories and counting: `SIGNUP`/`SUBMIT_LEAD_FORM` count one per click;
`PURCHASE` counts every conversion (the tool picks this by category). Set
`default_value` when conversions have a typical value, so value-based bidding
has something to work with.

## Mobile apps

App conversions cannot be created through this server. They are imported by
linking a source to the Google Ads account in the Google Ads UI (Tools >
Data manager):

- **Firebase / Google Analytics 4**: link the Firebase project or GA4
  property, then import events (first_open, purchase, custom events).
- **Google Play**: link the Play Console account for installs and in-app purchases.
- **Third-party app analytics** (AppsFlyer, Adjust, Branch, ...): link the
  provider.

Once linked, they appear in `list_conversion_actions` with types such as
`FIREBASE_ANDROID_FIRST_OPEN`, `GOOGLE_ANALYTICS_4_PURCHASE`,
`GOOGLE_PLAY_DOWNLOAD` or `THIRD_PARTY_APP_ANALYTICS_IOS_CUSTOM`. Pass their
IDs to `create_app_campaign(conversion_action_ids=[...])` for the
`IN_APP_ACTIONS` and `IN_APP_VALUE` goals.

If the user asks for an in-app goal and no such actions exist, explain the
linking step and offer an `INSTALLS` campaign meanwhile.

## Primary vs. secondary

`primary_for_goal=true` actions drive bidding; secondary ones are only
reported. Keep one or two meaningful primary actions (e.g. purchase or
qualified sign-up), and demote noisy ones (page views, clicks) with
`update_conversion_action(primary_for_goal=False)`. Confirm with the user
first: this changes what every campaign optimizes for.
