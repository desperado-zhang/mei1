from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .hashutil import canonical_json, sha256_json


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "sql" / "schema.sql"


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text())
        self._ensure_column("member_list_observations", "row_content_hash", "TEXT")
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_member_list_observations_content_hash
            ON member_list_observations (member_id, row_content_hash)
            """
        )
        self.conn.commit()

    def upsert_tenant(
        self,
        tenant_key: str,
        *,
        merchant_id: str | None = None,
        merchant_name: str | None = None,
        store_id: str | None = None,
        store_name: str | None = None,
        operator_name: str | None = None,
        source_account_label: str | None = None,
        raw_json: dict[str, Any] | None = None,
    ) -> str:
        self.conn.execute(
            """
            INSERT INTO tenant_contexts (
              tenant_key, merchant_id, merchant_name, store_id, store_name,
              operator_name, source_account_label, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_key) DO UPDATE SET
              merchant_id = COALESCE(excluded.merchant_id, tenant_contexts.merchant_id),
              merchant_name = COALESCE(excluded.merchant_name, tenant_contexts.merchant_name),
              store_id = COALESCE(excluded.store_id, tenant_contexts.store_id),
              store_name = COALESCE(excluded.store_name, tenant_contexts.store_name),
              operator_name = COALESCE(excluded.operator_name, tenant_contexts.operator_name),
              source_account_label = COALESCE(excluded.source_account_label, tenant_contexts.source_account_label),
              raw_json = COALESCE(excluded.raw_json, tenant_contexts.raw_json),
              last_seen_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (
                tenant_key,
                merchant_id,
                merchant_name,
                store_id,
                store_name,
                operator_name,
                source_account_label,
                _json(raw_json),
            ),
        )
        self.conn.commit()
        return tenant_key

    def start_run(self, *, tenant_key: str | None, entry_url: str, mode: str = "playwright") -> int:
        cur = self.conn.execute(
            """
            INSERT INTO crawl_runs (tenant_key, entry_url, mode)
            VALUES (?, ?, ?)
            """,
            (tenant_key, entry_url, mode),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_run(self, run_id: int, **values: Any) -> None:
        if not values:
            return
        allowed = {
            "tenant_key",
            "ended_at",
            "status",
            "login_wait_started_at",
            "login_wait_ended_at",
            "popup_close_wait_started_at",
            "popup_close_wait_ended_at",
            "page_count",
            "member_count",
            "notes",
        }
        keys = [key for key in values if key in allowed]
        if not keys:
            return
        assignments = ", ".join(f"{key} = ?" for key in keys)
        self.conn.execute(
            f"UPDATE crawl_runs SET {assignments} WHERE id = ?",
            [values[key] for key in keys] + [run_id],
        )
        self.conn.commit()

    def finish_run(self, run_id: int, status: str, notes: str | None = None) -> None:
        self.conn.execute(
            """
            UPDATE crawl_runs
            SET status = ?, notes = COALESCE(?, notes), ended_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (status, notes, run_id),
        )
        self.conn.commit()

    def add_event(self, run_id: int, event_type: str, message: str | None = None, payload: Any = None) -> None:
        self.conn.execute(
            """
            INSERT INTO crawl_events (run_id, event_type, message, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (run_id, event_type, message, _json(payload)),
        )
        self.conn.commit()

    def save_source_payload(
        self,
        *,
        run_id: int,
        tenant_key: str,
        page_area: str,
        method: str,
        endpoint: str,
        request_url: str,
        request_params: Any = None,
        request_body: Any = None,
        status_code: int | None = None,
        response_json: Any = None,
    ) -> int:
        request_fingerprint = sha256_json(
            {
                "tenant_key": tenant_key,
                "method": method.upper(),
                "endpoint": endpoint,
                "request_params": request_params,
                "request_body": request_body,
            }
        )
        response_sha = sha256_json(response_json)
        self.conn.execute(
            """
            INSERT OR IGNORE INTO source_payloads (
              run_id, tenant_key, page_area, method, endpoint, request_url,
              request_fingerprint, request_params_json, request_body_json,
              status_code, response_sha256, response_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                tenant_key,
                page_area,
                method.upper(),
                endpoint,
                request_url,
                request_fingerprint,
                _json(request_params),
                _json(request_body),
                status_code,
                response_sha,
                _json(response_json),
            ),
        )
        row = self.conn.execute(
            """
            SELECT id FROM source_payloads
            WHERE request_fingerprint = ? AND response_sha256 = ?
            """,
            (request_fingerprint, response_sha),
        ).fetchone()
        self.conn.commit()
        return int(row["id"])

    def save_list_page(
        self,
        *,
        run_id: int,
        tenant_key: str,
        page_no: int,
        page_size: int,
        total_count: int | None,
        sort_key: str | None,
        filters: Any,
        source_payload_id: int | None,
    ) -> int:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO list_pages (
              run_id, tenant_key, page_no, page_size, total_count,
              sort_key, filters_json, source_payload_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, tenant_key, page_no, page_size, total_count, sort_key, _json(filters), source_payload_id),
        )
        row = self.conn.execute(
            """
            SELECT id FROM list_pages
            WHERE run_id = ? AND page_no = ? AND page_size = ? AND COALESCE(sort_key, '') = COALESCE(?, '')
            """,
            (run_id, page_no, page_size, sort_key),
        ).fetchone()
        self.conn.commit()
        return int(row["id"])

    def upsert_member(self, member: dict[str, Any]) -> int:
        columns = [
            "tenant_key",
            "source_member_id",
            "member_no",
            "name",
            "gender",
            "mobile_masked",
            "mobile_sha256",
            "wechat_account",
            "wechat_bound",
            "qq",
            "email",
            "grade_name",
            "member_layer",
            "source_channel",
            "store_id",
            "store_name",
            "tracking_employee_id",
            "tracking_employee_name",
            "exclusive_advisor_id",
            "exclusive_advisor_name",
            "referrer_member_id",
            "referrer_name",
            "occupation",
            "height_cm",
            "weight_kg",
            "blood_type",
            "address",
            "birthday_type",
            "birthday_date",
            "next_birthday_date",
            "age",
            "age_group",
            "joined_at",
            "note",
            "raw_profile_json",
        ]
        values = [self._db_value(member.get(column)) for column in columns]
        placeholders = ", ".join("?" for _ in columns)
        assignments = ", ".join(
            f"{column} = COALESCE(excluded.{column}, members.{column})"
            for column in columns
            if column != "tenant_key"
        )

        conflict_key = None
        if member.get("source_member_id"):
            conflict_key = "source"
            conflict_clause = "ON CONFLICT(tenant_key, source_member_id) WHERE source_member_id IS NOT NULL AND source_member_id <> ''"
        elif member.get("member_no"):
            conflict_key = "member_no"
            conflict_clause = """
                ON CONFLICT(tenant_key, member_no)
                WHERE (source_member_id IS NULL OR source_member_id = '')
                  AND member_no IS NOT NULL
                  AND member_no <> ''
            """
        else:
            conflict_clause = None

        if conflict_clause:
            sql = f"""
                INSERT INTO members ({", ".join(columns)})
                VALUES ({placeholders})
                {conflict_clause}
                DO UPDATE SET
                  {assignments},
                  last_seen_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """
            self.conn.execute(sql, values)
        else:
            self.conn.execute(
                f"INSERT INTO members ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )

        if conflict_key == "source":
            row = self.conn.execute(
                "SELECT id FROM members WHERE tenant_key = ? AND source_member_id = ?",
                (member["tenant_key"], member["source_member_id"]),
            ).fetchone()
        elif conflict_key == "member_no":
            row = self.conn.execute(
                """
                SELECT id FROM members
                WHERE tenant_key = ?
                  AND member_no = ?
                  AND (source_member_id IS NULL OR source_member_id = '')
                """,
                (member["tenant_key"], member["member_no"]),
            ).fetchone()
        else:
            row = self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()
        self.conn.commit()
        return int(row["id"])

    def save_list_observation(
        self,
        *,
        run_id: int,
        tenant_key: str,
        list_page_id: int | None,
        member_id: int | None,
        row: dict[str, Any],
        row_index: int,
    ) -> None:
        columns = [
            "run_id",
            "tenant_key",
            "list_page_id",
            "member_id",
            "row_index",
            "row_fingerprint",
            "row_content_hash",
            "source_member_id",
            "member_no",
            "name",
            "mobile_masked",
            "grade_name",
            "card_count",
            "stored_value_balance_cents",
            "total_consume_cents",
            "total_visit_count",
            "current_month_visit_count",
            "last_consume_at",
            "last_service_employee_name",
            "last_consume_amount_cents",
            "raw_row_json",
        ]
        payload = {
            "run_id": run_id,
            "tenant_key": tenant_key,
            "list_page_id": list_page_id,
            "member_id": member_id,
            "row_index": row_index,
            **row,
        }
        self.conn.execute(
            f"""
            INSERT OR IGNORE INTO member_list_observations ({", ".join(columns)})
            VALUES ({", ".join("?" for _ in columns)})
            """,
            [self._db_value(payload.get(column)) for column in columns],
        )
        self.conn.commit()

    def record_member_scan_state(
        self,
        *,
        run_id: int,
        tenant_key: str,
        member_id: int,
        row: dict[str, Any],
    ) -> str:
        current_hash = row.get("row_content_hash")
        if not current_hash:
            return "unknown"

        existing = self.conn.execute(
            """
            SELECT last_list_content_hash
            FROM member_sync_states
            WHERE member_id = ?
            """,
            (member_id,),
        ).fetchone()
        source_member_id = row.get("source_member_id")
        member_no = row.get("member_no")
        raw_row_json = row.get("raw_row_json")

        if existing is None:
            self.conn.execute(
                """
                INSERT INTO member_sync_states (
                  member_id, tenant_key, source_member_id, member_no,
                  last_list_content_hash, first_seen_run_id, last_seen_run_id,
                  last_changed_run_id, list_seen_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (member_id, tenant_key, source_member_id, member_no, current_hash, run_id, run_id, run_id),
            )
            self._insert_member_change_event(
                run_id=run_id,
                tenant_key=tenant_key,
                member_id=member_id,
                source_member_id=source_member_id,
                member_no=member_no,
                change_type="new",
                previous_hash=None,
                current_hash=current_hash,
                raw_row_json=raw_row_json,
            )
            self.conn.commit()
            return "new"

        previous_hash = existing["last_list_content_hash"]
        if previous_hash != current_hash:
            self.conn.execute(
                """
                UPDATE member_sync_states
                SET source_member_id = COALESCE(?, source_member_id),
                    member_no = COALESCE(?, member_no),
                    last_list_content_hash = ?,
                    list_seen_count = list_seen_count + 1,
                    last_seen_run_id = ?,
                    last_changed_run_id = ?,
                    last_seen_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                    last_changed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE member_id = ?
                """,
                (source_member_id, member_no, current_hash, run_id, run_id, member_id),
            )
            self._insert_member_change_event(
                run_id=run_id,
                tenant_key=tenant_key,
                member_id=member_id,
                source_member_id=source_member_id,
                member_no=member_no,
                change_type="changed",
                previous_hash=previous_hash,
                current_hash=current_hash,
                raw_row_json=raw_row_json,
            )
            self.conn.commit()
            return "changed"

        self.conn.execute(
            """
            UPDATE member_sync_states
            SET source_member_id = COALESCE(?, source_member_id),
                member_no = COALESCE(?, member_no),
                list_seen_count = list_seen_count + 1,
                last_seen_run_id = ?,
                last_seen_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE member_id = ?
            """,
            (source_member_id, member_no, run_id, member_id),
        )
        self.conn.commit()
        return "unchanged"

    def mark_detail_requested(self, member_id: int, reason: str) -> None:
        self.conn.execute(
            """
            UPDATE member_sync_states
            SET last_detail_reason = ?,
                detail_requested_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE member_id = ?
            """,
            (reason, member_id),
        )
        self.conn.commit()

    def rebuild_sync_state_from_observations(self) -> int:
        rows = self.conn.execute(
            """
            SELECT
              o.member_id,
              o.tenant_key,
              o.source_member_id,
              o.member_no,
              o.row_content_hash,
              o.run_id,
              o.observed_at
            FROM member_list_observations o
            JOIN (
              SELECT member_id, max(observed_at) AS max_observed_at
              FROM member_list_observations
              WHERE member_id IS NOT NULL
                AND row_content_hash IS NOT NULL
              GROUP BY member_id
            ) latest
              ON latest.member_id = o.member_id
             AND latest.max_observed_at = o.observed_at
            WHERE o.member_id IS NOT NULL
              AND o.row_content_hash IS NOT NULL
            """
        ).fetchall()
        for row in rows:
            self.conn.execute(
                """
                INSERT INTO member_sync_states (
                  member_id, tenant_key, source_member_id, member_no,
                  last_list_content_hash, first_seen_run_id, last_seen_run_id,
                  last_changed_run_id, first_seen_at, last_seen_at, last_changed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(member_id) DO UPDATE SET
                  tenant_key = excluded.tenant_key,
                  source_member_id = COALESCE(excluded.source_member_id, member_sync_states.source_member_id),
                  member_no = COALESCE(excluded.member_no, member_sync_states.member_no),
                  last_list_content_hash = excluded.last_list_content_hash,
                  last_seen_run_id = excluded.last_seen_run_id,
                  last_seen_at = excluded.last_seen_at
                """,
                (
                    row["member_id"],
                    row["tenant_key"],
                    row["source_member_id"],
                    row["member_no"],
                    row["row_content_hash"],
                    row["run_id"],
                    row["run_id"],
                    row["run_id"],
                    row["observed_at"],
                    row["observed_at"],
                    row["observed_at"],
                ),
            )
        self.conn.commit()
        return len(rows)

    def backfill_list_content_hashes(self, *, force: bool = False) -> int:
        from .parser import normalize_list_observation

        predicate = "raw_row_json IS NOT NULL" if force else "row_content_hash IS NULL AND raw_row_json IS NOT NULL"
        rows = self.conn.execute(
            f"""
            SELECT id, tenant_key, row_index, raw_row_json
            FROM member_list_observations
            WHERE {predicate}
            """
        ).fetchall()
        for row in rows:
            raw_row = json.loads(row["raw_row_json"])
            normalized = normalize_list_observation(raw_row, row["tenant_key"], int(row["row_index"]))
            self.conn.execute(
                """
                UPDATE member_list_observations
                SET row_content_hash = ?
                WHERE id = ?
                """,
                (normalized.get("row_content_hash"), row["id"]),
            )
        self.conn.commit()
        return len(rows)

    def sync_state_counts(self) -> dict[str, int]:
        return {
            "member_sync_states": int(self.conn.execute("SELECT count(*) FROM member_sync_states").fetchone()[0]),
            "member_change_events": int(self.conn.execute("SELECT count(*) FROM member_change_events").fetchone()[0]),
        }

    def save_asset_snapshot(self, run_id: int, snapshot: dict[str, Any]) -> None:
        columns = [
            "run_id",
            "member_id",
            "snapshot_hash",
            "member_wallet_cents",
            "remaining_consume_value_cents",
            "points",
            "debt_cents",
            "card_count",
            "coupon_count",
            "total_consume_cents",
            "total_card_consumed_cents",
            "referral_count",
            "current_year_consume_rank",
            "lifetime_consume_rank",
            "total_visit_count",
            "average_visit_interval_days",
            "lifecycle_category",
            "partner_store_balance_cents",
            "partner_withdrawable_cents",
            "direct_referrer_count",
            "indirect_referrer_count",
            "raw_json",
        ]
        payload = {"run_id": run_id, **snapshot}
        self.conn.execute(
            f"""
            INSERT OR IGNORE INTO member_asset_snapshots ({", ".join(columns)})
            VALUES ({", ".join("?" for _ in columns)})
            """,
            [self._db_value(payload.get(column)) for column in columns],
        )
        self.conn.commit()

    def save_account_item(self, item: dict[str, Any]) -> int:
        columns = [
            "member_id",
            "item_scope",
            "source_item_id",
            "item_no",
            "item_name",
            "item_type",
            "status",
            "source_name",
            "valid_from",
            "valid_to",
            "is_permanent",
            "deal_price_cents",
            "remaining_times_text",
            "balance_cents",
            "display_balance_text",
            "raw_json",
        ]
        values = [self._db_value(item.get(column)) for column in columns]
        placeholders = ", ".join("?" for _ in columns)
        assignments = ", ".join(
            f"{column} = COALESCE(excluded.{column}, member_account_items.{column})"
            for column in columns
            if column not in {"member_id", "item_scope", "source_item_id", "item_no"}
        )

        if item.get("source_item_id"):
            conflict_clause = """
                ON CONFLICT(member_id, item_scope, source_item_id)
                WHERE source_item_id IS NOT NULL AND source_item_id <> ''
            """
        elif item.get("item_no"):
            conflict_clause = """
                ON CONFLICT(member_id, item_scope, item_no)
                WHERE item_no IS NOT NULL AND item_no <> ''
            """
        else:
            conflict_clause = None

        if conflict_clause:
            self.conn.execute(
                f"""
                INSERT INTO member_account_items ({", ".join(columns)})
                VALUES ({placeholders})
                {conflict_clause}
                DO UPDATE SET
                  {assignments},
                  last_seen_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                values,
            )
        else:
            self.conn.execute(
                f"INSERT INTO member_account_items ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )

        if item.get("source_item_id"):
            row = self.conn.execute(
                """
                SELECT id FROM member_account_items
                WHERE member_id = ? AND item_scope = ? AND source_item_id = ?
                """,
                (item["member_id"], item["item_scope"], item["source_item_id"]),
            ).fetchone()
        elif item.get("item_no"):
            row = self.conn.execute(
                """
                SELECT id FROM member_account_items
                WHERE member_id = ? AND item_scope = ? AND item_no = ?
                """,
                (item["member_id"], item["item_scope"], item["item_no"]),
            ).fetchone()
        else:
            row = self.conn.execute("SELECT last_insert_rowid() AS id").fetchone()
        self.conn.commit()
        return int(row["id"])

    def save_service_record(self, record: dict[str, Any]) -> int:
        columns = [
            "member_id",
            "source_record_id",
            "record_type",
            "record_at",
            "employee_id",
            "employee_name",
            "content",
            "related_items_text",
            "record_hash",
            "raw_json",
        ]
        self.conn.execute(
            f"""
            INSERT INTO member_service_records ({", ".join(columns)})
            VALUES ({", ".join("?" for _ in columns)})
            ON CONFLICT(member_id, record_hash) DO UPDATE SET
              source_record_id = COALESCE(excluded.source_record_id, member_service_records.source_record_id),
              record_type = excluded.record_type,
              record_at = COALESCE(excluded.record_at, member_service_records.record_at),
              employee_id = COALESCE(excluded.employee_id, member_service_records.employee_id),
              employee_name = COALESCE(excluded.employee_name, member_service_records.employee_name),
              content = COALESCE(excluded.content, member_service_records.content),
              related_items_text = COALESCE(excluded.related_items_text, member_service_records.related_items_text),
              raw_json = COALESCE(excluded.raw_json, member_service_records.raw_json),
              last_seen_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            [self._db_value(record.get(column)) for column in columns],
        )
        row = self.conn.execute(
            """
            SELECT id FROM member_service_records
            WHERE member_id = ? AND record_hash = ?
            """,
            (record["member_id"], record["record_hash"]),
        ).fetchone()
        self.conn.commit()
        return int(row["id"])

    def save_detail_record(self, record: dict[str, Any]) -> int:
        columns = [
            "member_id",
            "category",
            "source_record_id",
            "happened_at",
            "title",
            "status",
            "amount_cents",
            "store_id",
            "store_name",
            "employee_id",
            "employee_name",
            "room_name",
            "content",
            "order_no",
            "duration_minutes",
            "record_hash",
            "raw_json",
        ]
        self.conn.execute(
            f"""
            INSERT INTO member_detail_records ({", ".join(columns)})
            VALUES ({", ".join("?" for _ in columns)})
            ON CONFLICT(member_id, category, record_hash) DO UPDATE SET
              source_record_id = COALESCE(excluded.source_record_id, member_detail_records.source_record_id),
              happened_at = COALESCE(excluded.happened_at, member_detail_records.happened_at),
              title = COALESCE(excluded.title, member_detail_records.title),
              status = COALESCE(excluded.status, member_detail_records.status),
              amount_cents = COALESCE(excluded.amount_cents, member_detail_records.amount_cents),
              store_id = COALESCE(excluded.store_id, member_detail_records.store_id),
              store_name = COALESCE(excluded.store_name, member_detail_records.store_name),
              employee_id = COALESCE(excluded.employee_id, member_detail_records.employee_id),
              employee_name = COALESCE(excluded.employee_name, member_detail_records.employee_name),
              room_name = COALESCE(excluded.room_name, member_detail_records.room_name),
              content = COALESCE(excluded.content, member_detail_records.content),
              order_no = COALESCE(excluded.order_no, member_detail_records.order_no),
              duration_minutes = COALESCE(excluded.duration_minutes, member_detail_records.duration_minutes),
              raw_json = COALESCE(excluded.raw_json, member_detail_records.raw_json),
              last_seen_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            [self._db_value(record.get(column)) for column in columns],
        )
        row = self.conn.execute(
            """
            SELECT id FROM member_detail_records
            WHERE member_id = ? AND category = ? AND record_hash = ?
            """,
            (record["member_id"], record["category"], record["record_hash"]),
        ).fetchone()
        self.conn.commit()
        return int(row["id"])

    def save_survey_profile(self, profile: dict[str, Any]) -> int:
        columns = [
            "member_id",
            "source_profile_id",
            "profile_name",
            "profile_url",
            "field_values_json",
            "raw_json",
        ]
        self.conn.execute(
            f"""
            INSERT INTO member_survey_profiles ({", ".join(columns)})
            VALUES ({", ".join("?" for _ in columns)})
            ON CONFLICT(member_id, source_profile_id) DO UPDATE SET
              profile_name = COALESCE(excluded.profile_name, member_survey_profiles.profile_name),
              profile_url = COALESCE(excluded.profile_url, member_survey_profiles.profile_url),
              field_values_json = COALESCE(excluded.field_values_json, member_survey_profiles.field_values_json),
              raw_json = COALESCE(excluded.raw_json, member_survey_profiles.raw_json),
              last_seen_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            [self._db_value(profile.get(column)) for column in columns],
        )
        row = self.conn.execute(
            """
            SELECT id FROM member_survey_profiles
            WHERE member_id = ? AND source_profile_id = ?
            """,
            (profile["member_id"], profile["source_profile_id"]),
        ).fetchone()
        self.conn.commit()
        return int(row["id"])

    def save_attachment(self, attachment: dict[str, Any]) -> int:
        columns = [
            "member_id",
            "source_file_id",
            "file_name",
            "content_type",
            "file_url_hash",
            "note",
            "uploaded_at",
            "raw_json",
        ]
        self.conn.execute(
            f"""
            INSERT INTO member_attachments ({", ".join(columns)})
            VALUES ({", ".join("?" for _ in columns)})
            ON CONFLICT(member_id, source_file_id) DO UPDATE SET
              file_name = COALESCE(excluded.file_name, member_attachments.file_name),
              content_type = COALESCE(excluded.content_type, member_attachments.content_type),
              file_url_hash = COALESCE(excluded.file_url_hash, member_attachments.file_url_hash),
              note = COALESCE(excluded.note, member_attachments.note),
              uploaded_at = COALESCE(excluded.uploaded_at, member_attachments.uploaded_at),
              raw_json = COALESCE(excluded.raw_json, member_attachments.raw_json),
              last_seen_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            [self._db_value(attachment.get(column)) for column in columns],
        )
        row = self.conn.execute(
            """
            SELECT id FROM member_attachments
            WHERE member_id = ? AND source_file_id = ?
            """,
            (attachment["member_id"], attachment["source_file_id"]),
        ).fetchone()
        self.conn.commit()
        return int(row["id"])

    def save_partner_info(self, info: dict[str, Any]) -> int:
        columns = [
            "member_id",
            "partner_member_id",
            "partner_level",
            "store_balance_cents",
            "withdrawable_cents",
            "direct_referrer_name",
            "indirect_referrer_name",
            "raw_json",
        ]
        self.conn.execute(
            f"""
            INSERT INTO member_partner_infos ({", ".join(columns)})
            VALUES ({", ".join("?" for _ in columns)})
            ON CONFLICT(member_id, partner_member_id) DO UPDATE SET
              partner_level = COALESCE(excluded.partner_level, member_partner_infos.partner_level),
              store_balance_cents = COALESCE(excluded.store_balance_cents, member_partner_infos.store_balance_cents),
              withdrawable_cents = COALESCE(excluded.withdrawable_cents, member_partner_infos.withdrawable_cents),
              direct_referrer_name = COALESCE(excluded.direct_referrer_name, member_partner_infos.direct_referrer_name),
              indirect_referrer_name = COALESCE(excluded.indirect_referrer_name, member_partner_infos.indirect_referrer_name),
              raw_json = COALESCE(excluded.raw_json, member_partner_infos.raw_json),
              last_seen_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            [self._db_value(info.get(column)) for column in columns],
        )
        row = self.conn.execute(
            """
            SELECT id FROM member_partner_infos
            WHERE member_id = ? AND partner_member_id = ?
            """,
            (info["member_id"], info["partner_member_id"]),
        ).fetchone()
        self.conn.commit()
        return int(row["id"])

    def find_member_id(
        self,
        *,
        tenant_key: str,
        source_member_id: str | None = None,
        member_no: str | None = None,
    ) -> int | None:
        row = None
        if source_member_id:
            row = self.conn.execute(
                "SELECT id FROM members WHERE tenant_key = ? AND source_member_id = ?",
                (tenant_key, source_member_id),
            ).fetchone()
        if row is None and member_no:
            row = self.conn.execute(
                "SELECT id FROM members WHERE tenant_key = ? AND member_no = ?",
                (tenant_key, member_no),
            ).fetchone()
        return int(row["id"]) if row else None

    def mark_member_detail_seen(self, member_id: int) -> None:
        self.conn.execute(
            """
            UPDATE members
            SET detail_last_seen_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                last_seen_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE id = ?
            """,
            (member_id,),
        )
        self.conn.commit()

    def counts(self) -> dict[str, int]:
        tables = [
            "crawl_runs",
            "source_payloads",
            "list_pages",
            "members",
            "member_list_observations",
            "member_asset_snapshots",
            "member_account_items",
            "member_service_records",
            "member_detail_records",
            "member_survey_profiles",
            "member_attachments",
            "member_partner_infos",
            "member_sync_states",
            "member_change_events",
        ]
        return {
            table: int(self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in tables
        }

    def _db_value(self, value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return _json(value)
        return value

    def _insert_member_change_event(
        self,
        *,
        run_id: int,
        tenant_key: str,
        member_id: int,
        source_member_id: str | None,
        member_no: str | None,
        change_type: str,
        previous_hash: str | None,
        current_hash: str,
        raw_row_json: Any,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO member_change_events (
              run_id, tenant_key, member_id, source_member_id, member_no,
              change_type, previous_hash, current_hash, raw_row_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                tenant_key,
                member_id,
                source_member_id,
                member_no,
                change_type,
                previous_hash,
                current_hash,
                _json(raw_row_json),
            ),
        )

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return canonical_json(value)
