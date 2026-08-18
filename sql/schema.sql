PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

INSERT INTO schema_meta (key, value)
VALUES ('schema_version', '2026-08-18.1')
ON CONFLICT(key) DO UPDATE SET
  value = excluded.value,
  updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now');

CREATE TABLE IF NOT EXISTS tenant_contexts (
  tenant_key TEXT PRIMARY KEY,
  merchant_id TEXT,
  merchant_name TEXT,
  store_id TEXT,
  store_name TEXT,
  operator_name TEXT,
  source_account_label TEXT,
  first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  last_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  raw_json TEXT CHECK (raw_json IS NULL OR json_valid(raw_json))
);

CREATE TABLE IF NOT EXISTS crawl_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_key TEXT REFERENCES tenant_contexts(tenant_key) ON UPDATE CASCADE,
  source_site TEXT NOT NULL DEFAULT 'saas.mei1.com',
  entry_url TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'playwright',
  started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  ended_at TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  login_wait_started_at TEXT,
  login_wait_ended_at TEXT,
  popup_close_wait_started_at TEXT,
  popup_close_wait_ended_at TEXT,
  page_count INTEGER NOT NULL DEFAULT 0,
  member_count INTEGER NOT NULL DEFAULT 0,
  notes TEXT,
  CHECK (status IN ('running', 'waiting_login', 'waiting_popup_close', 'success', 'failed', 'cancelled'))
);

CREATE TABLE IF NOT EXISTS crawl_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  message TEXT,
  payload_json TEXT CHECK (payload_json IS NULL OR json_valid(payload_json)),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS source_payloads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER REFERENCES crawl_runs(id) ON DELETE SET NULL,
  tenant_key TEXT REFERENCES tenant_contexts(tenant_key) ON UPDATE CASCADE,
  page_area TEXT NOT NULL,
  method TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  request_url TEXT,
  request_fingerprint TEXT NOT NULL,
  request_params_json TEXT CHECK (request_params_json IS NULL OR json_valid(request_params_json)),
  request_body_json TEXT CHECK (request_body_json IS NULL OR json_valid(request_body_json)),
  status_code INTEGER,
  response_sha256 TEXT,
  response_json TEXT CHECK (response_json IS NULL OR json_valid(response_json)),
  captured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  UNIQUE (request_fingerprint, response_sha256)
);

CREATE TABLE IF NOT EXISTS list_pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
  tenant_key TEXT NOT NULL REFERENCES tenant_contexts(tenant_key) ON UPDATE CASCADE,
  page_no INTEGER NOT NULL,
  page_size INTEGER NOT NULL,
  total_count INTEGER,
  sort_key TEXT,
  filters_json TEXT CHECK (filters_json IS NULL OR json_valid(filters_json)),
  source_payload_id INTEGER REFERENCES source_payloads(id) ON DELETE SET NULL,
  fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  UNIQUE (run_id, page_no, page_size, sort_key)
);

CREATE TABLE IF NOT EXISTS members (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_key TEXT NOT NULL REFERENCES tenant_contexts(tenant_key) ON UPDATE CASCADE,
  source_member_id TEXT,
  member_no TEXT,
  local_sequence_no TEXT,
  name TEXT,
  name_pinyin TEXT,
  gender TEXT,
  mobile_masked TEXT,
  mobile_sha256 TEXT,
  mobile_encrypted TEXT,
  wechat_account TEXT,
  wechat_bound INTEGER,
  qq TEXT,
  email TEXT,
  id_card_no_encrypted TEXT,
  grade_name TEXT,
  member_layer TEXT,
  source_channel TEXT,
  store_id TEXT,
  store_name TEXT,
  tracking_employee_id TEXT,
  tracking_employee_name TEXT,
  exclusive_advisor_id TEXT,
  exclusive_advisor_name TEXT,
  referrer_member_id TEXT,
  referrer_name TEXT,
  occupation TEXT,
  height_cm REAL,
  weight_kg REAL,
  blood_type TEXT,
  address TEXT,
  birthday_type TEXT,
  birthday_date TEXT,
  next_birthday_date TEXT,
  age INTEGER,
  age_group TEXT,
  joined_at TEXT,
  note TEXT,
  first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  last_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  detail_last_seen_at TEXT,
  raw_profile_json TEXT CHECK (raw_profile_json IS NULL OR json_valid(raw_profile_json))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_members_source_id
ON members (tenant_key, source_member_id)
WHERE source_member_id IS NOT NULL AND source_member_id <> '';

CREATE UNIQUE INDEX IF NOT EXISTS ux_members_member_no
ON members (tenant_key, member_no)
WHERE member_no IS NOT NULL AND member_no <> '';

CREATE INDEX IF NOT EXISTS ix_members_name ON members (tenant_key, name);
CREATE INDEX IF NOT EXISTS ix_members_mobile_hash ON members (tenant_key, mobile_sha256);

CREATE TABLE IF NOT EXISTS member_list_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
  tenant_key TEXT NOT NULL REFERENCES tenant_contexts(tenant_key) ON UPDATE CASCADE,
  list_page_id INTEGER REFERENCES list_pages(id) ON DELETE SET NULL,
  member_id INTEGER REFERENCES members(id) ON DELETE SET NULL,
  row_index INTEGER NOT NULL,
  row_fingerprint TEXT NOT NULL,
  source_member_id TEXT,
  member_no TEXT,
  name TEXT,
  mobile_masked TEXT,
  grade_name TEXT,
  card_count INTEGER,
  stored_value_balance_cents INTEGER,
  total_consume_cents INTEGER,
  total_visit_count INTEGER,
  current_month_visit_count INTEGER,
  last_consume_at TEXT,
  last_service_employee_name TEXT,
  last_consume_amount_cents INTEGER,
  raw_row_json TEXT CHECK (raw_row_json IS NULL OR json_valid(raw_row_json)),
  observed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  UNIQUE (run_id, row_fingerprint)
);

CREATE INDEX IF NOT EXISTS ix_member_list_observations_member
ON member_list_observations (member_id, observed_at);

CREATE TABLE IF NOT EXISTS member_tags (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  tag_type TEXT NOT NULL,
  tag_name TEXT NOT NULL,
  source_tag_id TEXT,
  color TEXT,
  first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  last_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  raw_json TEXT CHECK (raw_json IS NULL OR json_valid(raw_json)),
  UNIQUE (member_id, tag_type, tag_name)
);

CREATE TABLE IF NOT EXISTS member_asset_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER REFERENCES crawl_runs(id) ON DELETE SET NULL,
  member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  snapshot_hash TEXT NOT NULL,
  member_wallet_cents INTEGER,
  remaining_consume_value_cents INTEGER,
  points INTEGER,
  debt_cents INTEGER,
  card_count INTEGER,
  coupon_count INTEGER,
  total_consume_cents INTEGER,
  total_card_consumed_cents INTEGER,
  referral_count INTEGER,
  current_year_consume_rank INTEGER,
  lifetime_consume_rank INTEGER,
  total_visit_count INTEGER,
  average_visit_interval_days REAL,
  lifecycle_category TEXT,
  partner_store_balance_cents INTEGER,
  partner_withdrawable_cents INTEGER,
  direct_referrer_count INTEGER,
  indirect_referrer_count INTEGER,
  observed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  raw_json TEXT CHECK (raw_json IS NULL OR json_valid(raw_json)),
  UNIQUE (member_id, snapshot_hash)
);

CREATE TABLE IF NOT EXISTS member_account_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  item_scope TEXT NOT NULL,
  source_item_id TEXT,
  item_no TEXT,
  item_name TEXT,
  item_type TEXT,
  status TEXT,
  source_name TEXT,
  valid_from TEXT,
  valid_to TEXT,
  is_permanent INTEGER,
  deal_price_cents INTEGER,
  remaining_times_text TEXT,
  balance_cents INTEGER,
  display_balance_text TEXT,
  first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  last_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  raw_json TEXT CHECK (raw_json IS NULL OR json_valid(raw_json)),
  CHECK (item_scope IN ('held_card', 'coupon', 'present', 'mall_coupon', 'transferred', 'deposit_item'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_member_account_items_source
ON member_account_items (member_id, item_scope, source_item_id)
WHERE source_item_id IS NOT NULL AND source_item_id <> '';

CREATE UNIQUE INDEX IF NOT EXISTS ux_member_account_items_no
ON member_account_items (member_id, item_scope, item_no)
WHERE item_no IS NOT NULL AND item_no <> '';

CREATE INDEX IF NOT EXISTS ix_member_account_items_member
ON member_account_items (member_id, item_scope, last_seen_at);

CREATE TABLE IF NOT EXISTS member_service_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  source_record_id TEXT,
  record_type TEXT NOT NULL,
  record_at TEXT,
  employee_id TEXT,
  employee_name TEXT,
  content TEXT,
  related_items_text TEXT,
  record_hash TEXT NOT NULL,
  first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  last_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  raw_json TEXT CHECK (raw_json IS NULL OR json_valid(raw_json)),
  CHECK (record_type IN ('return_visit', 'service_note', 'development_plan', 'other')),
  UNIQUE (member_id, record_hash)
);

CREATE INDEX IF NOT EXISTS ix_member_service_records_time
ON member_service_records (member_id, record_at);

CREATE TABLE IF NOT EXISTS member_detail_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  category TEXT NOT NULL,
  source_record_id TEXT,
  happened_at TEXT,
  title TEXT,
  status TEXT,
  amount_cents INTEGER,
  store_id TEXT,
  store_name TEXT,
  employee_id TEXT,
  employee_name TEXT,
  room_name TEXT,
  content TEXT,
  order_no TEXT,
  duration_minutes INTEGER,
  record_hash TEXT NOT NULL,
  first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  last_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  raw_json TEXT CHECK (raw_json IS NULL OR json_valid(raw_json)),
  CHECK (category IN (
    'appointment',
    'consume',
    'gift',
    'points',
    'modification',
    'mall_order',
    'face_scan',
    'reach_store',
    'wallet',
    'deposit',
    'other'
  )),
  UNIQUE (member_id, category, record_hash)
);

CREATE INDEX IF NOT EXISTS ix_member_detail_records_time
ON member_detail_records (member_id, category, happened_at);

CREATE TABLE IF NOT EXISTS member_survey_profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  source_profile_id TEXT,
  profile_name TEXT,
  profile_url TEXT,
  field_values_json TEXT CHECK (field_values_json IS NULL OR json_valid(field_values_json)),
  first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  last_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  raw_json TEXT CHECK (raw_json IS NULL OR json_valid(raw_json)),
  UNIQUE (member_id, source_profile_id)
);

CREATE TABLE IF NOT EXISTS member_attachments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  source_file_id TEXT,
  file_name TEXT,
  content_type TEXT,
  file_url_hash TEXT,
  note TEXT,
  uploaded_at TEXT,
  first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  last_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  raw_json TEXT CHECK (raw_json IS NULL OR json_valid(raw_json)),
  UNIQUE (member_id, source_file_id)
);

CREATE TABLE IF NOT EXISTS member_partner_infos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  partner_member_id TEXT,
  partner_level TEXT,
  store_balance_cents INTEGER,
  withdrawable_cents INTEGER,
  direct_referrer_name TEXT,
  indirect_referrer_name TEXT,
  first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  last_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  raw_json TEXT CHECK (raw_json IS NULL OR json_valid(raw_json)),
  UNIQUE (member_id, partner_member_id)
);
