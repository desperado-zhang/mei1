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
            conflict_clause = "ON CONFLICT(tenant_key, member_no) WHERE member_no IS NOT NULL AND member_no <> ''"
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
                "SELECT id FROM members WHERE tenant_key = ? AND member_no = ?",
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
        ]
        return {
            table: int(self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in tables
        }

    def _db_value(self, value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return _json(value)
        return value


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return canonical_json(value)
