from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qsl, urlparse

from playwright.sync_api import Page, Response

from .db import Database
from .parser import (
    LIST_ENDPOINT,
    account_scope_for_endpoint,
    detail_category_for_endpoint,
    endpoint_path,
    extract_data,
    extract_attachments,
    extract_account_items,
    extract_member_profile,
    extract_rows,
    extract_total_count,
    is_member_endpoint,
    normalize_account_item,
    normalize_asset_snapshot,
    normalize_attachment,
    normalize_detail_record,
    normalize_list_observation,
    normalize_member,
    normalize_partner_info,
    normalize_service_record,
    normalize_survey_profile,
    page_area_for_endpoint,
)


@dataclass
class CaptureStats:
    source_payloads: int = 0
    list_pages: int = 0
    list_rows: int = 0
    members_upserted: int = 0
    detail_profiles: int = 0
    asset_snapshots: int = 0
    account_items: int = 0
    service_records: int = 0
    detail_records: int = 0
    survey_profiles: int = 0
    attachments: int = 0
    partner_infos: int = 0
    new_members: int = 0
    changed_members: int = 0
    unchanged_members: int = 0
    unknown_change_members: int = 0
    ignored_non_json: int = 0
    skipped_permission_denied: int = 0
    errors: list[str] = field(default_factory=list)


class ApiCapture:
    def __init__(self, db: Database, *, run_id: int, tenant_key: str) -> None:
        self.db = db
        self.run_id = run_id
        self.tenant_key = tenant_key
        self.stats = CaptureStats()
        self._lock = threading.RLock()
        self._seen_member_source_ids: dict[str, int] = {}
        self.list_page_source_member_ids: dict[int, list[str]] = {}
        self.detail_target_source_ids: list[str] = []
        self.detail_target_reasons: dict[str, str] = {}

    def attach(self, page: Page) -> None:
        page.on("response", self._on_response)

    def wait_for(self, predicate: Callable[[CaptureStats], bool], timeout_seconds: float) -> bool:
        deadline = None if timeout_seconds <= 0 else time.monotonic() + timeout_seconds
        while True:
            with self._lock:
                if predicate(self.stats):
                    return True
            if deadline is not None and time.monotonic() > deadline:
                return False
            time.sleep(0.25)

    def _on_response(self, response: Response) -> None:
        try:
            path = endpoint_path(response.url)
            if not is_member_endpoint(path):
                return
            if response.status in {401, 403}:
                self._record_permission_skip(path, response.status)
                return
            request = response.request
            payload = self._response_json(response)
            if payload is None:
                with self._lock:
                    self.stats.ignored_non_json += 1
                return
            self.process_payload(
                method=request.method,
                endpoint=path,
                request_url=response.url,
                request_params=dict(parse_qsl(urlparse(response.url).query)),
                request_body=self._request_body(request),
                status_code=response.status,
                response_json=payload,
            )
        except Exception as exc:  # noqa: BLE001 - browser event callbacks must not crash the run.
            message = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self.stats.errors.append(message)
            self.db.add_event(self.run_id, "capture_error", message)

    def process_payload(
        self,
        *,
        method: str,
        endpoint: str,
        request_url: str,
        request_params: Any = None,
        request_body: Any = None,
        status_code: int | None = None,
        response_json: Any = None,
    ) -> None:
        path = endpoint_path(endpoint)
        if not is_member_endpoint(path):
            return
        if status_code in {401, 403}:
            self._record_permission_skip(path, status_code)
            return
        if _looks_permission_denied(response_json):
            self._record_permission_skip(path, status_code)
            return

        source_payload_id = self.db.save_source_payload(
            run_id=self.run_id,
            tenant_key=self.tenant_key,
            page_area=page_area_for_endpoint(path),
            method=method,
            endpoint=path,
            request_url=request_url,
            request_params=request_params,
            request_body=request_body,
            status_code=status_code,
            response_json=response_json,
        )
        with self._lock:
            self.stats.source_payloads += 1

        if path == LIST_ENDPOINT:
            self._save_member_list(response_json, request_body, source_payload_id)
        elif path.startswith(("/api/member/detailInfo/", "/api/member/detail/", "/api/member/memberAttr/")) or re.fullmatch(
            r"/api/member/[^/?#]+", path
        ):
            self._save_member_profile(path, response_json)
        elif path.startswith(("/api/member/amount/", "/api/member/queryMemberRemainConsumeValue/")):
            self._save_asset_snapshot(path, response_json)
        elif account_scope_for_endpoint(path, request_body):
            self._save_account_items(path, request_body, response_json)
        elif path == "/api/wechatbusinessassists/memberServiceList":
            self._save_service_records(path, request_body, response_json)
        elif detail_category_for_endpoint(path, request_body):
            self._save_detail_records(path, request_body, response_json)
        elif path.startswith("/api/memberSurveys/profile/") or path == "/api/tduckDataProxy/query":
            self._save_survey_profiles(path, request_body, response_json)
        elif path.startswith(("/api/storePartner/getStorePartnerByMemberId/", "/api/storePartnerAccount/queryByMemberId/")):
            self._save_partner_info(path, request_body, response_json)

    def _record_permission_skip(self, path: str, status_code: int | None) -> None:
        with self._lock:
            self.stats.skipped_permission_denied += 1
        self.db.add_event(self.run_id, "permission_denied", f"Skipped permission-denied endpoint: {path}", {"status": status_code})

    def _save_member_list(self, payload: Any, request_body: Any, source_payload_id: int) -> None:
        rows = extract_rows(payload)
        page_no = _first_int_from_any(request_body, "page", "pageNo", "page_no", "index") or 1
        page_size = _first_int_from_any(request_body, "size", "pageSize", "page_size", "limit") or len(rows) or 20
        total_count = extract_total_count(payload)
        list_page_id = self.db.save_list_page(
            run_id=self.run_id,
            tenant_key=self.tenant_key,
            page_no=page_no,
            page_size=page_size,
            total_count=total_count,
            sort_key=_first_str_from_any(request_body, "sort", "sortKey", "orderBy"),
            filters=request_body,
            source_payload_id=source_payload_id,
        )
        for index, row in enumerate(rows):
            member = normalize_member(row, self.tenant_key)
            member_id = self.db.upsert_member(member)
            source_member_id = member.get("source_member_id")
            if source_member_id:
                source_id = str(source_member_id)
                self._seen_member_source_ids[source_id] = member_id
                self.list_page_source_member_ids.setdefault(page_no, []).append(source_id)
            observation = normalize_list_observation(row, self.tenant_key, index)
            self.db.save_list_observation(
                run_id=self.run_id,
                tenant_key=self.tenant_key,
                list_page_id=list_page_id,
                member_id=member_id,
                row=observation,
                row_index=index,
            )
            change_type = self.db.record_member_scan_state(
                run_id=self.run_id,
                tenant_key=self.tenant_key,
                member_id=member_id,
                row=observation,
            )
            if change_type in {"new", "changed"} and source_member_id:
                if source_id not in self.detail_target_reasons:
                    self.detail_target_source_ids.append(source_id)
                    self.detail_target_reasons[source_id] = change_type
            with self._lock:
                if change_type == "new":
                    self.stats.new_members += 1
                elif change_type == "changed":
                    self.stats.changed_members += 1
                elif change_type == "unchanged":
                    self.stats.unchanged_members += 1
                else:
                    self.stats.unknown_change_members += 1
        with self._lock:
            self.stats.list_pages += 1
            self.stats.list_rows += len(rows)
            self.stats.members_upserted += len(rows)

    def _save_member_profile(self, path: str, payload: Any) -> None:
        profile = extract_member_profile(payload)
        if not profile:
            return
        source_id = _member_id_from_path(path)
        if source_id and not any(key in profile for key in ("id", "memberId", "member_id")):
            profile = {**profile, "id": source_id}
        member = normalize_member(profile, self.tenant_key)
        member_id = self.db.upsert_member(member)
        self.db.mark_member_detail_seen(member_id)
        for attachment_row in extract_attachments(payload):
            attachment = normalize_attachment(attachment_row, member_id)
            self.db.save_attachment(attachment)
            with self._lock:
                self.stats.attachments += 1
        if source_id:
            self._seen_member_source_ids[str(source_id)] = member_id
        with self._lock:
            self.stats.detail_profiles += 1
            self.stats.members_upserted += 1

    def _save_asset_snapshot(self, path: str, payload: Any) -> None:
        source_id = _member_id_from_path(path)
        if not source_id:
            return
        member_id = self._seen_member_source_ids.get(str(source_id)) or self.db.find_member_id(
            tenant_key=self.tenant_key,
            source_member_id=str(source_id),
        )
        if member_id is None:
            return
        snapshot = normalize_asset_snapshot(payload, member_id)
        self.db.save_asset_snapshot(self.run_id, snapshot)
        with self._lock:
            self.stats.asset_snapshots += 1

    def _save_account_items(self, path: str, request_body: Any, payload: Any) -> None:
        source_id = self._member_source_id_for_payload(path, request_body, payload)
        member_id = self._member_id_for_source(source_id)
        if member_id is None:
            return
        item_scope = account_scope_for_endpoint(path, request_body)
        if not item_scope:
            return
        rows = extract_account_items(payload) or extract_rows(payload)
        for account_row in rows:
            account_item = normalize_account_item(account_row, member_id, item_scope)
            self.db.save_account_item(account_item)
            with self._lock:
                self.stats.account_items += 1

    def _save_service_records(self, path: str, request_body: Any, payload: Any) -> None:
        source_id = self._member_source_id_for_payload(path, request_body, payload)
        member_id = self._member_id_for_source(source_id)
        if member_id is None:
            return
        for row in extract_rows(payload):
            record = normalize_service_record(row, member_id)
            self.db.save_service_record(record)
            with self._lock:
                self.stats.service_records += 1

    def _save_detail_records(self, path: str, request_body: Any, payload: Any) -> None:
        source_id = self._member_source_id_for_payload(path, request_body, payload)
        member_id = self._member_id_for_source(source_id)
        category = detail_category_for_endpoint(path, request_body)
        if member_id is None or not category:
            return
        for row in extract_rows(payload):
            record = normalize_detail_record(row, member_id, category)
            self.db.save_detail_record(record)
            with self._lock:
                self.stats.detail_records += 1

    def _save_survey_profiles(self, path: str, request_body: Any, payload: Any) -> None:
        source_id = self._member_source_id_for_payload(path, request_body, payload)
        member_id = self._member_id_for_source(source_id)
        if member_id is None:
            return
        rows = extract_rows(payload)
        if not rows:
            data = extract_data(payload)
            rows = [data] if isinstance(data, dict) and data else []
        for row in rows:
            profile = normalize_survey_profile(row, member_id)
            self.db.save_survey_profile(profile)
            with self._lock:
                self.stats.survey_profiles += 1

    def _save_partner_info(self, path: str, request_body: Any, payload: Any) -> None:
        source_id = self._member_source_id_for_payload(path, request_body, payload)
        member_id = self._member_id_for_source(source_id)
        if member_id is None:
            return
        info = normalize_partner_info(payload, member_id)
        if info is None:
            return
        self.db.save_partner_info(info)
        with self._lock:
            self.stats.partner_infos += 1

    def _member_id_for_source(self, source_id: str | None) -> int | None:
        if not source_id:
            return None
        return self._seen_member_source_ids.get(str(source_id)) or self.db.find_member_id(
            tenant_key=self.tenant_key,
            source_member_id=str(source_id),
        )

    def _member_source_id_for_payload(self, path: str, request_body: Any, payload: Any) -> str | None:
        return _member_id_from_path(path) or _first_str_from_any(request_body, "memberId", "holderId") or _first_str_from_any(
            payload,
            "memberId",
            "id",
        )

    def _response_json(self, response: Response) -> Any | None:
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower() and not response.url.endswith(".json"):
            return None
        return response.json()

    def _request_body(self, request: Any) -> Any:
        try:
            return request.post_data_json
        except Exception:
            pass
        data = request.post_data
        if not data:
            return None
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return {"raw": data}


def _member_id_from_path(path: str) -> str | None:
    match = re.search(
        r"/api/(?:"
        r"member/(?:detailInfo|detail|amount|memberAttr|queryMemberRemainConsumeValue)/"
        r"|member/"
        r"|memberSurveys/profile/"
        r"|storePartner/getStorePartnerByMemberId/"
        r"|storePartnerAccount/queryByMemberId/"
        r")([^/?#]+)$",
        path,
    )
    return match.group(1) if match else None


def _looks_permission_denied(payload: Any) -> bool:
    text = _payload_text(payload)
    return bool(re.search(r"无权限|没有.*权限|权限不足|未授权|未登录|unauthorized|forbidden|permission denied", text, re.I))


def _payload_text(value: Any) -> str:
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, dict):
        parts: list[str] = []
        for key, child in value.items():
            if key in {"message", "msg", "error", "errorMessage", "tips", "reason"}:
                parts.append(_payload_text(child))
            elif isinstance(child, (dict, list)):
                parts.append(_payload_text(child))
        return " ".join(part for part in parts if part)[:2000]
    if isinstance(value, list):
        return " ".join(_payload_text(item) for item in value[:20])[:2000]
    return ""


def _first_int_from_any(value: Any, *keys: str) -> int | None:
    found = _first_from_any(value, *keys)
    if found is None:
        return None
    if isinstance(found, int):
        return found
    text = str(found).replace(",", "")
    match = re.search(r"-?\d+", text)
    return int(match.group(0)) if match else None


def _first_str_from_any(value: Any, *keys: str) -> str | None:
    found = _first_from_any(value, *keys)
    return str(found).strip() if found not in (None, "") else None


def _first_from_any(value: Any, *keys: str) -> Any:
    if isinstance(value, dict):
        if value.get("field") in keys and value.get("value") not in (None, ""):
            return value.get("value")
        for key in keys:
            if key in value and value[key] not in (None, ""):
                return value[key]
        for child in value.values():
            found = _first_from_any(child, *keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_from_any(child, *keys)
            if found is not None:
                return found
    return None
