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
    endpoint_path,
    extract_member_profile,
    extract_rows,
    extract_total_count,
    is_member_endpoint,
    normalize_asset_snapshot,
    normalize_list_observation,
    normalize_member,
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
            if _looks_permission_denied(payload):
                self._record_permission_skip(path, response.status)
                return

            request_body = self._request_body(request)
            source_payload_id = self.db.save_source_payload(
                run_id=self.run_id,
                tenant_key=self.tenant_key,
                page_area=page_area_for_endpoint(path),
                method=request.method,
                endpoint=path,
                request_url=response.url,
                request_params=dict(parse_qsl(urlparse(response.url).query)),
                request_body=request_body,
                status_code=response.status,
                response_json=payload,
            )
            with self._lock:
                self.stats.source_payloads += 1

            if path == LIST_ENDPOINT:
                self._save_member_list(payload, request_body, source_payload_id)
            elif path.startswith(("/api/member/detailInfo/", "/api/member/detail/")):
                self._save_member_profile(path, payload)
            elif path.startswith("/api/member/amount/"):
                self._save_asset_snapshot(path, payload)
        except Exception as exc:  # noqa: BLE001 - browser event callbacks must not crash the run.
            message = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self.stats.errors.append(message)
            self.db.add_event(self.run_id, "capture_error", message)

    def _record_permission_skip(self, path: str, status_code: int) -> None:
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
            if member.get("source_member_id"):
                self._seen_member_source_ids[str(member["source_member_id"])] = member_id
            observation = normalize_list_observation(row, self.tenant_key, index)
            self.db.save_list_observation(
                run_id=self.run_id,
                tenant_key=self.tenant_key,
                list_page_id=list_page_id,
                member_id=member_id,
                row=observation,
                row_index=index,
            )
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
    match = re.search(r"/api/member/(?:detailInfo|detail|amount)/([^/?#]+)", path)
    return match.group(1) if match else None


def _looks_permission_denied(payload: Any) -> bool:
    text = _payload_text(payload)
    return bool(re.search(r"无权限|没有权限|权限不足|未授权|未登录|unauthorized|forbidden|permission denied", text, re.I))


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
