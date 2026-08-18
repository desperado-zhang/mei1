# Mei1 customer crawler data model

## Scope

Current scope is customer list data and customer detail drawer data from:

`https://saas.mei1.com/app/#/member-new/list?index=0&page=1`

Observed page behavior:

- The app is an Angular SPA.
- The list page remains on the same route while the detail drawer opens.
- The detail drawer component is exposed in the public HTML as `mw-member-template` with `member-id`, `tab-index`, and `descendants-index`.
- The visible list is a custom content list, not a conventional HTML table.
- Values rendered in the UI can use icon fonts or masking. API JSON should be preferred over visible text when both are available.

Default v1 excludes WeChat chat content. The tab exists in the detail drawer, but chat records are communication data and should be added only after explicit opt-in. Attachment files are also excluded from runtime download; only metadata can be modeled.

## Runtime constraints

- Every run and debug command must activate the local Miniconda environment first.
- Python + SQLite is the persistence layer.
- Python + Playwright is the default browser layer.
- ego-lite can also be used after manual login. The current `crawl-ego` path calls the Angular front-end `api.member` service in the logged-in page and then sends the resulting JSON through the same SQLite ingest path.
- If Playwright detects an unauthenticated page, it should flash or focus the browser tab and wait until the user logs in and closes popups.
- The crawler should deduplicate before writing, using stable source IDs when available and content hashes as fallback.
- If a customer endpoint returns no-permission or unauthorized status/text, skip it and record a runtime event instead of storing the response payload.

## Main tables

### `tenant_contexts`

Stores the merchant/store/login context. Use `tenant_key` as the local stable scope key even when the source merchant/store IDs are unavailable at first.

Recommended `tenant_key`:

- If API reveals IDs: `merchant:{merchant_id}:store:{store_id}`
- Otherwise: hash of visible merchant/store/operator labels

### `crawl_runs` and `crawl_events`

Tracks one execution. These tables record login waiting, popup waiting, success/failure, page count, member count, and notable runtime events.

### `source_payloads`

Raw request/response capture. This is the replay and audit layer.

Use it for intercepted APIs such as:

- `POST /api/member/list/search`
- `GET /api/member/detailInfo/{memberId}`
- `GET /api/member/detail/{memberId}`
- `GET /api/member/{memberId}`
- `GET /api/member/memberAttr/{memberId}`
- `GET /api/member/queryMemberRemainConsumeValue/{memberId}`
- `POST /api/member/list/cardAndPresent`
- `POST /api/couponUser/memberCouponSearch`
- `POST /api/giveTradeRecord/giveFirendSearch`
- `POST /api/deposit/depositStock/searchStockListData`
- `POST /api/wechatbusinessassists/memberServiceList`
- `POST /api/member/list/record`
- `GET /api/member/amount/{memberId}` when authorized
- `POST /api/pointsChangeRecord/search`
- `POST /api/mallItemTrade/mallMemberTrade`
- `POST /api/dragonflyBrushFace/brushFaceRecord`
- `POST /api/member/reachStore/record`
- `POST /api/deposit/depositOperateRecord/searchRecordListData`
- `GET /api/memberSurveys/profile/{memberId}`
- `POST /api/tduckDataProxy/query`

### `list_pages`

One row per fetched list page. Stores page number, page size, total count, sort key, filters, and the source payload pointer.

### `members`

Current normalized customer profile.

Important keys:

- `source_member_id`: preferred source-side ID from API.
- `member_no`: visible member number, useful fallback.
- `mobile_sha256` / `mobile_encrypted`: for full mobile values if the API returns them. Do not store plaintext mobile by default.
- `raw_profile_json`: source profile payload for fields not yet normalized.

### `member_list_observations`

Append-only list-row observations per run. This preserves what each list page showed at crawl time, including ranking/order and row-level values.

`row_content_hash` is the stable incremental fingerprint. It is built from normalized business fields such as member ID, member number, masked mobile, grade, card count, consume totals, visit counts, and last consume fields. It intentionally excludes page number, row index, and volatile encrypted display helpers such as `mobile_encryption`.

### `member_sync_states` and `member_change_events`

`member_sync_states` stores the latest known `row_content_hash` for each member. A sampled scan compares the current list hash with this table:

- missing state: `new`
- different hash: `changed`
- same hash: `unchanged`

`member_change_events` records `new` and `changed` detections. Incremental detail fetching is driven by these detections, so unchanged sampled rows do not trigger detail-page API calls.

### `member_asset_snapshots`

Append-only snapshots for changing metrics:

- wallet
- remaining consume value
- points
- debt
- card/coupon count
- cumulative consumption
- cumulative card cost
- referral count
- consume ranks
- visit count and frequency
- lifecycle category
- partner balances

Rows are deduplicated by `(member_id, snapshot_hash)`.

### `member_account_items`

Cards, coupons, gifts, mall coupons, transferred items, and deposit items from the `会员帐户` tab.

The ego-lite implementation calls the visible account sub-tab APIs directly and paginates them:

- `held_card`, `coupon`, `present`: `POST /api/member/list/cardAndPresent`
- `mall_coupon`: `POST /api/couponUser/memberCouponSearch`
- `transferred`: `POST /api/giveTradeRecord/giveFirendSearch`
- `deposit_item`: `POST /api/deposit/depositStock/searchStockListData`

Balance-like source fields can mean either times or money, so v1 preserves the display text and raw JSON, and only fills `balance_cents` when the source category indicates a stored-value card.

Use `item_scope` to distinguish:

- `held_card`
- `coupon`
- `present`
- `mall_coupon`
- `transferred`
- `deposit_item`

### `member_service_records`

Records from `服务记录`:

- `return_visit`
- `service_note`
- `development_plan`
- `other`

Deduplication uses `(member_id, record_hash)` so rows without source IDs can still be stable.

The service-record endpoint name contains `wechatbusinessassists`, but it is the customer service-record tab, not WeChat chat history. Chat/conversation/message endpoints remain excluded.

### `member_detail_records`

Generic table for `顾客数据明细` sub-tabs:

- `appointment`
- `consume`
- `gift`
- `points`
- `modification`
- `mall_order`
- `face_scan`
- `reach_store`
- `wallet`
- `deposit`
- `other`

This keeps v1 schema stable even if each sub-tab has slightly different columns. Sub-tab-specific fields stay in `raw_json`.

### Optional detail tables

- `member_survey_profiles`: `美问问卷`
- `member_attachments`: `客户附件` metadata only, no automatic file download in v1
- `member_partner_infos`: `合伙人信息`
- `member_tags`: profile tags and taboo/custom tags

## Deduplication rules

Preferred order:

1. Source stable ID, for example member ID, card ID, record ID, file ID.
2. Business key, for example `(tenant_key, member_no)` or `(member_id, item_scope, item_no)`.
3. Content hash fallback, using a canonical JSON serialization of important fields.

For member upsert:

- Prefer unique index `ux_members_source_id`.
- If no source ID is available, use `ux_members_member_no`; do not let `member_no` override a stable source member ID.
- Update `last_seen_at` on every successful observation.
- Update normalized fields only when the new source value is non-empty.

For list rows:

- Build `row_fingerprint` from tenant key, source member ID or member number, page filters, and normalized visible row content.
- Store every run observation, but avoid duplicates inside the same run.

For records:

- If the source record has an ID, include it in `record_hash`.
- Otherwise hash category, timestamp, employee, amount, status, and content.

## Money and dates

- Store money in integer cents.
- Store source display text in `raw_json` or display text columns when values are masked or font-obfuscated.
- Store dates as ISO-like text. Normalize source formats such as `2026/08/29 12:30:00` to `2026-08-29T12:30:00+08:00` during parsing.

## Privacy defaults

- Do not store plaintext full phone numbers by default.
- If the API returns full phone numbers, store `mobile_sha256`; only store `mobile_encrypted` after an encryption key strategy is agreed.
- Do not scrape or store WeChat chat contents in v1.
- Attachments are metadata-only unless later explicitly requested.
