from __future__ import annotations

import csv
import html
import io
import json
import math
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse


DEFAULT_PAGE_SIZE = 20
PAGE_SIZE_OPTIONS = (20, 50, 100)
MAX_EXPORT_ROWS = 100_000
ACCOUNT_PAGE_SIZE = 100
RECORD_PAGE_SIZE = 50

DETAIL_TABS = (
    ("account", "会员账户"),
    ("profile", "会员资料"),
    ("customer_data", "客户数据"),
    ("service", "服务记录"),
    ("detail_records", "顾客数据明细"),
    ("survey", "美问问卷"),
    ("partner", "合伙人信息"),
    ("attachments", "客户附件"),
)

ACCOUNT_SCOPE_ORDER = ("held_card", "coupon", "present", "mall_coupon", "transferred", "deposit_item")
DETAIL_CATEGORY_ORDER = (
    "appointment",
    "consume",
    "gift",
    "modification",
    "wallet",
    "points",
    "mall_order",
    "face_scan",
    "reach_store",
    "deposit",
    "other",
)
SERVICE_TYPE_ORDER = ("return_visit", "service_note", "development_plan", "other")


@dataclass(frozen=True)
class CustomerFilters:
    name: str = ""
    member_no: str = ""
    mobile: str = ""
    store: str = ""
    grade: str = ""
    joined_from: str = ""
    joined_to: str = ""
    seen_from: str = ""
    seen_to: str = ""
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE


EXPORT_COLUMNS = (
    ("source_member_id", "源客户ID"),
    ("member_no", "会员号"),
    ("name", "姓名"),
    ("gender", "性别"),
    ("mobile_masked", "手机号掩码"),
    ("wechat_account", "微信号"),
    ("wechat_bound", "微信绑定"),
    ("grade_name", "会员等级"),
    ("member_layer", "客户分层"),
    ("source_channel", "来源渠道"),
    ("store_name", "门店"),
    ("tracking_employee_name", "跟踪员工"),
    ("exclusive_advisor_name", "专属顾问"),
    ("referrer_name", "推荐人"),
    ("occupation", "职业"),
    ("birthday_type", "生日类型"),
    ("birthday_date", "生日"),
    ("next_birthday_date", "下次生日"),
    ("age", "年龄"),
    ("age_group", "年龄段"),
    ("address", "地址"),
    ("joined_at", "注册日期"),
    ("first_seen_at", "首次采集时间"),
    ("last_seen_at", "最后采集时间"),
    ("detail_last_seen_at", "详情采集时间"),
    ("note", "备注"),
)


def serve_viewer(db_path: Path, host: str, port: int) -> None:
    resolved_db_path = db_path.expanduser().resolve()
    if not resolved_db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {resolved_db_path}")

    handler = _make_handler(resolved_db_path)
    with ThreadingHTTPServer((host, port), handler) as httpd:
        actual_host, actual_port = httpd.server_address
        if actual_host in {"", "0.0.0.0"}:
            actual_host = "127.0.0.1"
        print(
            "[mei1-viewer] serving read-only "
            f"db={resolved_db_path} url=http://{actual_host}:{actual_port}/members"
        )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[mei1-viewer] stopped")


def _make_handler(db_path: Path) -> type[BaseHTTPRequestHandler]:
    class CustomerViewerHandler(BaseHTTPRequestHandler):
        server_version = "Mei1CustomerViewer/0.1"

        def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
            parsed = urlparse(self.path)
            if parsed.path in {"/export.csv", "/member-export.csv"}:
                content_type = "text/csv; charset=utf-8"
            elif parsed.path == "/healthz":
                content_type = "text/plain; charset=utf-8"
            elif parsed.path == "/favicon.ico":
                content_type = "image/x-icon"
            elif parsed.path == "/" or parsed.path == "/members" or parsed.path.startswith("/members/"):
                content_type = "text/html; charset=utf-8"
            else:
                self.send_response(HTTPStatus.NOT_FOUND.value)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(HTTPStatus.OK.value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query, keep_blank_values=True)
            if parsed.path == "/":
                self._redirect("/members")
                return
            if parsed.path == "/healthz":
                self._send_bytes(b"ok\n", "text/plain; charset=utf-8")
                return
            if parsed.path == "/favicon.ico":
                self._send_bytes(b"", "image/x-icon", status=HTTPStatus.NO_CONTENT)
                return
            if parsed.path == "/members":
                self._send_html(_render_members_page(db_path, params))
                return
            if parsed.path == "/member-export.csv":
                body, filename, status = _render_member_tab_export_csv(db_path, params)
                headers = {"Content-Disposition": f'attachment; filename="{filename}"'} if status == HTTPStatus.OK else None
                self._send_bytes(body, "text/csv; charset=utf-8", status=status, headers=headers)
                return
            if parsed.path.startswith("/members/"):
                member_id = _parse_member_id(parsed.path)
                if member_id is None:
                    self._send_html(_render_not_found("客户ID无效"), status=HTTPStatus.NOT_FOUND)
                    return
                html_body, status = _render_member_detail_page(db_path, member_id, params)
                self._send_html(html_body, status=status)
                return
            if parsed.path == "/export.csv":
                body, filename = _render_export_csv(db_path, params)
                self._send_bytes(
                    body,
                    "text/csv; charset=utf-8",
                    headers={
                        "Content-Disposition": f'attachment; filename="{filename}"',
                    },
                )
                return
            self._send_html(_render_not_found("页面不存在"), status=HTTPStatus.NOT_FOUND)

        def log_message(self, format: str, *args: Any) -> None:
            print(f"[mei1-viewer] {self.address_string()} {format % args}")

        def _send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send_bytes(body.encode("utf-8"), "text/html; charset=utf-8", status=status)

        def _send_bytes(
            self,
            body: bytes,
            content_type: str,
            *,
            status: HTTPStatus = HTTPStatus.OK,
            headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status.value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location: str) -> None:
            self.send_response(HTTPStatus.FOUND.value)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return CustomerViewerHandler


def _render_members_page(db_path: Path, params: dict[str, list[str]]) -> str:
    filters = _parse_filters(params)
    with _connect_readonly(db_path) as conn:
        stats = _load_stats(conn, db_path)
        stores = _load_distinct_values(conn, "store_name")
        grades = _load_distinct_values(conn, "grade_name")
        rows, total, filters = _load_members(conn, filters)

    title = "客户浏览"
    export_href = "/export.csv"
    export_query = _query_string(filters, overrides={"page": ""})
    if export_query:
        export_href = f"{export_href}?{export_query}"

    content = f"""
    <section class="page-head">
      <div>
        <p class="eyebrow">本地 SQLite 只读查看</p>
        <h1>客户信息</h1>
      </div>
      <a class="button export" href="{_h(export_href)}">导出 CSV</a>
    </section>
    {_render_filter_form(filters, stores, grades)}
    <section class="summary-strip" aria-label="数据概览">
      <div><strong>{total}</strong><span>当前结果</span></div>
      <div><strong>{stats["member_count"]}</strong><span>客户总数</span></div>
      <div><strong>{stats["detail_seen_count"]}</strong><span>已采详情</span></div>
      <div><strong>{_h(_date_time(stats["last_seen_at"]))}</strong><span>最后采集</span></div>
    </section>
    <section class="data-panel">
      {_render_members_table(rows, filters)}
      {_render_pagination(filters, total)}
    </section>
    """
    return _layout(title, content, stats)


def _render_member_detail_page(
    db_path: Path,
    member_id: int,
    params: dict[str, list[str]],
) -> tuple[str, HTTPStatus]:
    active_tab = _detail_tab_from_params(params)
    account_scope = _scoped_param(params, "account_scope", ACCOUNT_SCOPE_ORDER)
    detail_category = _scoped_param(params, "detail_category", DETAIL_CATEGORY_ORDER)
    service_type = _scoped_param(params, "service_type", SERVICE_TYPE_ORDER)
    account_page = max(1, _int_param(params, "account_page", 1))
    detail_page = max(1, _int_param(params, "detail_page", 1))
    service_page = max(1, _int_param(params, "service_page", 1))

    with _connect_readonly(db_path) as conn:
        stats = _load_stats(conn, db_path)
        member = conn.execute(
            """
            SELECT
              m.*,
              (SELECT COUNT(*) FROM member_account_items i WHERE i.member_id = m.id) AS account_count,
              (SELECT COUNT(*) FROM member_service_records r WHERE r.member_id = m.id) AS service_count,
              (SELECT COUNT(*) FROM member_detail_records r WHERE r.member_id = m.id) AS detail_count,
              (SELECT COUNT(*) FROM member_survey_profiles s WHERE s.member_id = m.id) AS survey_count,
              (SELECT COUNT(*) FROM member_attachments a WHERE a.member_id = m.id) AS attachment_count,
              (SELECT COUNT(*) FROM member_partner_infos p WHERE p.member_id = m.id) AS partner_count,
              (SELECT COUNT(*) FROM member_tags t WHERE t.member_id = m.id) AS tag_count
            FROM members m
            WHERE m.id = ?
            """,
            (member_id,),
        ).fetchone()
        if member is None:
            return _layout("客户详情", _render_empty("未找到这个客户"), stats), HTTPStatus.NOT_FOUND

        account_counts = _group_counts(
            conn,
            "SELECT item_scope AS key, COUNT(*) AS count FROM member_account_items WHERE member_id = ? GROUP BY item_scope",
            (member_id,),
        )
        detail_counts = _group_counts(
            conn,
            "SELECT category AS key, COUNT(*) AS count FROM member_detail_records WHERE member_id = ? GROUP BY category",
            (member_id,),
        )
        service_counts = _group_counts(
            conn,
            "SELECT record_type AS key, COUNT(*) AS count FROM member_service_records WHERE member_id = ? GROUP BY record_type",
            (member_id,),
        )
        tags = conn.execute(
            """
            SELECT tag_type, tag_name, color, last_seen_at
            FROM member_tags
            WHERE member_id = ?
            ORDER BY tag_type, tag_name
            """,
            (member_id,),
        ).fetchall()
        asset_snapshots = conn.execute(
            """
            SELECT member_wallet_cents, remaining_consume_value_cents, points, debt_cents,
                   card_count, coupon_count, total_consume_cents, total_visit_count,
                   lifecycle_category, observed_at
            FROM member_asset_snapshots
            WHERE member_id = ?
            ORDER BY observed_at DESC, id DESC
            LIMIT 10
            """,
            (member_id,),
        ).fetchall()

        account_where = "member_id = ?"
        account_args: list[Any] = [member_id]
        if account_scope:
            account_where += " AND item_scope = ?"
            account_args.append(account_scope)
        account_total = _count_rows(conn, f"SELECT COUNT(*) FROM member_account_items WHERE {account_where}", account_args)
        account_page = _bounded_page(account_page, account_total, ACCOUNT_PAGE_SIZE)
        account_items = conn.execute(
            """
            SELECT item_scope, item_no, item_name, item_type, status, source_name,
                   valid_from, valid_to, remaining_times_text, balance_cents,
                   display_balance_text, last_seen_at
            FROM member_account_items
            WHERE """ + account_where + """
            ORDER BY item_scope, COALESCE(valid_to, last_seen_at) DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            [*account_args, ACCOUNT_PAGE_SIZE, (account_page - 1) * ACCOUNT_PAGE_SIZE],
        ).fetchall()

        service_where = "member_id = ?"
        service_args: list[Any] = [member_id]
        if service_type:
            service_where += " AND record_type = ?"
            service_args.append(service_type)
        service_total = _count_rows(conn, f"SELECT COUNT(*) FROM member_service_records WHERE {service_where}", service_args)
        service_page = _bounded_page(service_page, service_total, RECORD_PAGE_SIZE)
        service_records = conn.execute(
            """
            SELECT record_type, record_at, employee_name, content, related_items_text, last_seen_at
            FROM member_service_records
            WHERE """ + service_where + """
            ORDER BY COALESCE(record_at, last_seen_at) DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            [*service_args, RECORD_PAGE_SIZE, (service_page - 1) * RECORD_PAGE_SIZE],
        ).fetchall()

        detail_where = "member_id = ?"
        detail_args: list[Any] = [member_id]
        if detail_category:
            detail_where += " AND category = ?"
            detail_args.append(detail_category)
        detail_total = _count_rows(conn, f"SELECT COUNT(*) FROM member_detail_records WHERE {detail_where}", detail_args)
        detail_page = _bounded_page(detail_page, detail_total, RECORD_PAGE_SIZE)
        detail_records = conn.execute(
            """
            SELECT category, happened_at, title, status, amount_cents, store_name,
                   employee_name, room_name, content, order_no, duration_minutes, last_seen_at
            FROM member_detail_records
            WHERE """ + detail_where + """
            ORDER BY COALESCE(happened_at, last_seen_at) DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            [*detail_args, RECORD_PAGE_SIZE, (detail_page - 1) * RECORD_PAGE_SIZE],
        ).fetchall()
        survey_profiles = conn.execute(
            """
            SELECT source_profile_id, profile_name, profile_url, field_values_json, last_seen_at
            FROM member_survey_profiles
            WHERE member_id = ?
            ORDER BY last_seen_at DESC, id DESC
            LIMIT 50
            """,
            (member_id,),
        ).fetchall()
        attachments = conn.execute(
            """
            SELECT source_file_id, file_name, content_type, file_url_hash, note, uploaded_at, last_seen_at
            FROM member_attachments
            WHERE member_id = ?
            ORDER BY COALESCE(uploaded_at, last_seen_at) DESC, id DESC
            LIMIT 50
            """,
            (member_id,),
        ).fetchall()
        partner_infos = conn.execute(
            """
            SELECT partner_member_id, partner_level, store_balance_cents, withdrawable_cents,
                   direct_referrer_name, indirect_referrer_name, last_seen_at
            FROM member_partner_infos
            WHERE member_id = ?
            ORDER BY last_seen_at DESC, id DESC
            LIMIT 20
            """,
            (member_id,),
        ).fetchall()

    return_to = _safe_return_url(_first_param(params, "return")) or "/members"
    raw_summary = _raw_json_summary(member["raw_profile_json"])
    tab_counts = {
        "account": member["account_count"],
        "profile": member["tag_count"],
        "customer_data": len(asset_snapshots),
        "service": member["service_count"],
        "detail_records": member["detail_count"],
        "survey": member["survey_count"],
        "partner": member["partner_count"],
        "attachments": member["attachment_count"],
    }
    tab_content = _render_active_member_tab(
        member_id=member_id,
        member=member,
        return_to=return_to,
        active_tab=active_tab,
        tab_params=params,
        account_scope=account_scope,
        account_counts=account_counts,
        account_items=account_items,
        account_total=account_total,
        account_page=account_page,
        asset_snapshots=asset_snapshots,
        service_type=service_type,
        service_counts=service_counts,
        service_records=service_records,
        service_total=service_total,
        service_page=service_page,
        detail_category=detail_category,
        detail_counts=detail_counts,
        detail_records=detail_records,
        detail_total=detail_total,
        detail_page=detail_page,
        survey_profiles=survey_profiles,
        attachments=attachments,
        partner_infos=partner_infos,
        tags=tags,
        raw_summary=raw_summary,
    )
    content = f"""
    <section class="detail-head">
      <a class="button" href="{_h(return_to)}">返回列表</a>
      <div class="identity">
        <div class="avatar">{_h(_initials(member["name"]))}</div>
        <div>
          <p class="eyebrow">客户详情</p>
          <h1>{_h(member["name"] or "未命名客户")}</h1>
          <div class="muted-line">
            会员号 {_h(member["member_no"] or "-")} · 源ID {_h(member["source_member_id"] or "-")}
          </div>
        </div>
      </div>
      <div class="status-stack">
        {_badge(member["grade_name"] or "未分级", "grade")}
        {_badge("已采详情" if member["detail_last_seen_at"] else "未采详情", "ok" if member["detail_last_seen_at"] else "warn")}
      </div>
    </section>
    <section class="detail-grid">
      <article class="panel wide">
        <h2>基本信息</h2>
        {_render_key_values(_member_basic_pairs(member))}
      </article>
      <article class="panel">
        <h2>关联数据</h2>
        <div class="metric-grid">
          <div><strong>{member["account_count"]}</strong><span>账户项目</span></div>
          <div><strong>{member["service_count"]}</strong><span>服务记录</span></div>
          <div><strong>{member["detail_count"]}</strong><span>明细记录</span></div>
          <div><strong>{member["partner_count"]}</strong><span>合伙信息</span></div>
        </div>
      </article>
      <article class="panel">
        <h2>采集信息</h2>
        {_render_key_values((
            ("首次采集", _date_time(member["first_seen_at"])),
            ("最后采集", _date_time(member["last_seen_at"])),
            ("详情采集", _date_time(member["detail_last_seen_at"])),
            ("数据源", member["tenant_key"]),
        ))}
      </article>
    </section>
    {_render_detail_tab_nav(member_id, return_to, active_tab, tab_counts)}
    {tab_content}
    """
    return _layout("客户详情", content, stats), HTTPStatus.OK


def _render_active_member_tab(
    *,
    member_id: int,
    member: sqlite3.Row,
    return_to: str,
    active_tab: str,
    tab_params: dict[str, list[str]],
    account_scope: str,
    account_counts: dict[str, int],
    account_items: list[sqlite3.Row],
    account_total: int,
    account_page: int,
    asset_snapshots: list[sqlite3.Row],
    service_type: str,
    service_counts: dict[str, int],
    service_records: list[sqlite3.Row],
    service_total: int,
    service_page: int,
    detail_category: str,
    detail_counts: dict[str, int],
    detail_records: list[sqlite3.Row],
    detail_total: int,
    detail_page: int,
    survey_profiles: list[sqlite3.Row],
    attachments: list[sqlite3.Row],
    partner_infos: list[sqlite3.Row],
    tags: list[sqlite3.Row],
    raw_summary: str,
) -> str:
    export_href = _member_export_href(member_id, active_tab, tab_params)
    export_button = f'<a class="button export" href="{_h(export_href)}">导出当前 Tab CSV</a>'

    if active_tab == "profile":
        return f"""
        <section class="panel">
          <div class="section-head"><h2>会员资料</h2>{export_button}</div>
          {_render_key_values(_member_basic_pairs(member))}
          <h3>标签</h3>
          {_render_tags(tags)}
          <h3>原始资料摘要</h3>
          <div class="raw-summary">{_h(raw_summary)}</div>
        </section>
        """

    if active_tab == "customer_data":
        return f"""
        <section class="panel">
          <div class="section-head"><h2>客户数据</h2>{export_button}</div>
          <div class="metric-grid compact">
            <div><strong>{member["account_count"]}</strong><span>账户项目</span></div>
            <div><strong>{member["service_count"]}</strong><span>服务记录</span></div>
            <div><strong>{member["detail_count"]}</strong><span>明细记录</span></div>
            <div><strong>{_h(_date_time(member["detail_last_seen_at"]))}</strong><span>详情采集</span></div>
          </div>
          <h3>资产快照</h3>
          {_render_asset_snapshots(asset_snapshots)}
        </section>
        """

    if active_tab == "service":
        return f"""
        <section class="panel">
          <div class="section-head"><h2>服务记录</h2>{export_button}</div>
          {_render_scope_filter(
              member_id,
              return_to,
              "service",
              "service_type",
              service_type,
              service_counts,
              SERVICE_TYPE_ORDER,
              _record_type_label,
              "全部服务",
              "service_page",
          )}
          {_render_service_records(service_records)}
          {_render_table_pagination(
              total=service_total,
              page=service_page,
              page_size=RECORD_PAGE_SIZE,
              make_href=lambda page: _member_detail_href(
                  member_id,
                  return_to,
                  "service",
                  service_type=service_type,
                  service_page=page,
              ),
          )}
        </section>
        """

    if active_tab == "detail_records":
        return f"""
        <section class="panel">
          <div class="section-head"><h2>顾客数据明细</h2>{export_button}</div>
          {_render_scope_filter(
              member_id,
              return_to,
              "detail_records",
              "detail_category",
              detail_category,
              detail_counts,
              DETAIL_CATEGORY_ORDER,
              _detail_category_label,
              "全部明细",
              "detail_page",
          )}
          {_render_detail_records(detail_records)}
          {_render_table_pagination(
              total=detail_total,
              page=detail_page,
              page_size=RECORD_PAGE_SIZE,
              make_href=lambda page: _member_detail_href(
                  member_id,
                  return_to,
                  "detail_records",
                  detail_category=detail_category,
                  detail_page=page,
              ),
          )}
        </section>
        """

    if active_tab == "survey":
        return f"""
        <section class="panel">
          <div class="section-head"><h2>美问问卷</h2>{export_button}</div>
          {_render_survey_profiles(survey_profiles)}
        </section>
        """

    if active_tab == "partner":
        return f"""
        <section class="panel">
          <div class="section-head"><h2>合伙人信息</h2>{export_button}</div>
          {_render_partner_infos(partner_infos)}
        </section>
        """

    if active_tab == "attachments":
        return f"""
        <section class="panel">
          <div class="section-head"><h2>客户附件</h2>{export_button}</div>
          {_render_attachments(attachments)}
        </section>
        """

    return f"""
    <section class="panel">
      <div class="section-head"><h2>会员账户</h2>{export_button}</div>
      {_render_scope_filter(
          member_id,
          return_to,
          "account",
          "account_scope",
          account_scope,
          account_counts,
          ACCOUNT_SCOPE_ORDER,
          _item_scope_label,
          "全部账户",
          "account_page",
      )}
      {_render_account_items(account_items)}
      {_render_table_pagination(
          total=account_total,
          page=account_page,
          page_size=ACCOUNT_PAGE_SIZE,
          make_href=lambda page: _member_detail_href(
              member_id,
              return_to,
              "account",
              account_scope=account_scope,
              account_page=page,
          ),
      )}
    </section>
    """


def _render_detail_tab_nav(
    member_id: int,
    return_to: str,
    active_tab: str,
    tab_counts: dict[str, Any],
) -> str:
    links = []
    for tab, label in DETAIL_TABS:
        href = _member_detail_href(member_id, return_to, tab)
        count = tab_counts.get(tab)
        count_html = "" if count in (None, "") else f'<span class="pill-count">{int(count or 0)}</span>'
        classes = "tab-link active" if tab == active_tab else "tab-link"
        links.append(f'<a class="{classes}" href="{_h(href)}">{_h(label)}{count_html}</a>')
    return f'<nav class="tab-nav" aria-label="客户详情 Tab">{"".join(links)}</nav>'


def _render_scope_filter(
    member_id: int,
    return_to: str,
    tab: str,
    param_name: str,
    selected: str,
    counts: dict[str, int],
    order: tuple[str, ...],
    label_func: Any,
    all_label: str,
    page_param: str,
) -> str:
    items = [("", all_label, sum(counts.values()))]
    for key in _ordered_count_keys(counts, order):
        items.append((key, label_func(key), counts.get(key, 0)))

    chips = []
    for key, label, count in items:
        extra = {param_name: key, page_param: 1}
        href = _member_detail_href(member_id, return_to, tab, **extra)
        active = " active" if key == selected else ""
        chips.append(
            f'<a class="scope-chip{active}" href="{_h(href)}">{_h(label)}<span class="pill-count">{count}</span></a>'
        )
    return f'<div class="scope-tabs">{"".join(chips)}</div>'


def _render_table_pagination(
    *,
    total: int,
    page: int,
    page_size: int,
    make_href: Any,
) -> str:
    if total <= page_size:
        return f'<div class="table-meta">显示 {min(total, page_size)} / {total}</div>'
    page_count = max(1, math.ceil(total / page_size))
    current_page = min(max(1, page), page_count)
    start = (current_page - 1) * page_size + 1
    end = min(total, current_page * page_size)
    prev_html = (
        f'<a class="page-link" href="{_h(make_href(current_page - 1))}">上一页</a>'
        if current_page > 1
        else '<span class="page-link disabled">上一页</span>'
    )
    next_html = (
        f'<a class="page-link" href="{_h(make_href(current_page + 1))}">下一页</a>'
        if current_page < page_count
        else '<span class="page-link disabled">下一页</span>'
    )
    return f"""
    <div class="table-pager">
      <span>显示 {start}-{end} / {total}</span>
      <nav>{prev_html}<span class="page-link current">{current_page}/{page_count}</span>{next_html}</nav>
    </div>
    """


def _render_export_csv(db_path: Path, params: dict[str, list[str]]) -> tuple[bytes, str]:
    filters = _parse_filters(params)
    where_sql, args = _where_clause(filters)
    sql = f"""
        SELECT {", ".join(name for name, _label in EXPORT_COLUMNS)}
        FROM members m
        {where_sql}
        ORDER BY COALESCE(m.last_seen_at, m.first_seen_at) DESC, m.id DESC
        LIMIT ?
    """
    with _connect_readonly(db_path) as conn:
        rows = conn.execute(sql, [*args, MAX_EXPORT_ROWS]).fetchall()

    output = io.StringIO(newline="")
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow([label for _name, label in EXPORT_COLUMNS])
    for row in rows:
        writer.writerow([_csv_value(row[name], name) for name, _label in EXPORT_COLUMNS])

    filename = f"mei1_customers_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return output.getvalue().encode("utf-8"), filename


def _render_member_tab_export_csv(
    db_path: Path,
    params: dict[str, list[str]],
) -> tuple[bytes, str, HTTPStatus]:
    member_id = _int_param(params, "member_id", 0)
    if member_id <= 0:
        return _csv_error("member_id 无效"), "mei1_member_export_error.csv", HTTPStatus.BAD_REQUEST

    tab = _detail_tab_from_params(params)
    account_scope = _scoped_param(params, "account_scope", ACCOUNT_SCOPE_ORDER)
    detail_category = _scoped_param(params, "detail_category", DETAIL_CATEGORY_ORDER)
    service_type = _scoped_param(params, "service_type", SERVICE_TYPE_ORDER)

    with _connect_readonly(db_path) as conn:
        member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
        if member is None:
            return _csv_error("未找到客户"), "mei1_member_export_error.csv", HTTPStatus.NOT_FOUND

        output = io.StringIO(newline="")
        output.write("\ufeff")
        writer = csv.writer(output)

        if tab == "profile":
            writer.writerow(["字段", "值"])
            for label, value in _member_basic_pairs(member):
                writer.writerow([label, _display(value)])
            tag_rows = conn.execute(
                "SELECT tag_name FROM member_tags WHERE member_id = ? ORDER BY tag_type, tag_name",
                (member_id,),
            ).fetchall()
            writer.writerow(["标签", "；".join(str(row["tag_name"]) for row in tag_rows) if tag_rows else ""])
            writer.writerow(["原始资料摘要", _raw_json_summary(member["raw_profile_json"])])
        elif tab == "customer_data":
            writer.writerow(["时间", "钱包", "剩余消费", "积分", "欠款", "卡数", "到店", "生命周期"])
            rows = conn.execute(
                """
                SELECT member_wallet_cents, remaining_consume_value_cents, points, debt_cents,
                       card_count, total_visit_count, lifecycle_category, observed_at
                FROM member_asset_snapshots
                WHERE member_id = ?
                ORDER BY observed_at DESC, id DESC
                LIMIT ?
                """,
                (member_id, MAX_EXPORT_ROWS),
            ).fetchall()
            for row in rows:
                writer.writerow(
                    [
                        _date_time(row["observed_at"]),
                        _money(row["member_wallet_cents"]),
                        _money(row["remaining_consume_value_cents"]),
                        _display(row["points"]),
                        _money(row["debt_cents"]),
                        _display(row["card_count"]),
                        _display(row["total_visit_count"]),
                        _display(row["lifecycle_category"]),
                    ]
                )
        elif tab == "service":
            service_where = "member_id = ?"
            service_args: list[Any] = [member_id]
            if service_type:
                service_where += " AND record_type = ?"
                service_args.append(service_type)
            writer.writerow(["类型", "时间", "员工", "内容", "关联项目", "最后采集"])
            rows = conn.execute(
                """
                SELECT record_type, record_at, employee_name, content, related_items_text, last_seen_at
                FROM member_service_records
                WHERE """ + service_where + """
                ORDER BY COALESCE(record_at, last_seen_at) DESC, id DESC
                LIMIT ?
                """,
                [*service_args, MAX_EXPORT_ROWS],
            ).fetchall()
            for row in rows:
                writer.writerow(
                    [
                        _record_type_label(row["record_type"]),
                        _date_time(row["record_at"]),
                        _display(row["employee_name"]),
                        _display(row["content"]),
                        _display(row["related_items_text"]),
                        _date_time(row["last_seen_at"]),
                    ]
                )
        elif tab == "detail_records":
            detail_where = "member_id = ?"
            detail_args: list[Any] = [member_id]
            if detail_category:
                detail_where += " AND category = ?"
                detail_args.append(detail_category)
            writer.writerow(["分类", "时间", "标题", "单号", "门店", "房间", "员工", "金额", "时长分钟", "状态", "内容"])
            rows = conn.execute(
                """
                SELECT category, happened_at, title, order_no, store_name, room_name,
                       employee_name, amount_cents, duration_minutes, status, content
                FROM member_detail_records
                WHERE """ + detail_where + """
                ORDER BY COALESCE(happened_at, last_seen_at) DESC, id DESC
                LIMIT ?
                """,
                [*detail_args, MAX_EXPORT_ROWS],
            ).fetchall()
            for row in rows:
                writer.writerow(
                    [
                        _detail_category_label(row["category"]),
                        _date_time(row["happened_at"]),
                        _display(row["title"]),
                        _display(row["order_no"]),
                        _display(row["store_name"]),
                        _display(row["room_name"]),
                        _display(row["employee_name"]),
                        _money(row["amount_cents"]),
                        _display(row["duration_minutes"]),
                        _display(row["status"]),
                        _display(row["content"]),
                    ]
                )
        elif tab == "survey":
            writer.writerow(["问卷ID", "问卷名称", "问卷地址", "字段摘要", "最后采集"])
            rows = conn.execute(
                """
                SELECT source_profile_id, profile_name, profile_url, field_values_json, last_seen_at
                FROM member_survey_profiles
                WHERE member_id = ?
                ORDER BY last_seen_at DESC, id DESC
                LIMIT ?
                """,
                (member_id, MAX_EXPORT_ROWS),
            ).fetchall()
            for row in rows:
                writer.writerow(
                    [
                        _display(row["source_profile_id"]),
                        _display(row["profile_name"]),
                        _display(row["profile_url"]),
                        _json_value_summary(row["field_values_json"]),
                        _date_time(row["last_seen_at"]),
                    ]
                )
        elif tab == "partner":
            writer.writerow(["合伙人ID", "等级", "店铺余额", "可提现", "直接推荐人", "间接推荐人", "最后采集"])
            rows = conn.execute(
                """
                SELECT partner_member_id, partner_level, store_balance_cents, withdrawable_cents,
                       direct_referrer_name, indirect_referrer_name, last_seen_at
                FROM member_partner_infos
                WHERE member_id = ?
                ORDER BY last_seen_at DESC, id DESC
                LIMIT ?
                """,
                (member_id, MAX_EXPORT_ROWS),
            ).fetchall()
            for row in rows:
                writer.writerow(
                    [
                        _display(row["partner_member_id"]),
                        _display(row["partner_level"]),
                        _money(row["store_balance_cents"]),
                        _money(row["withdrawable_cents"]),
                        _display(row["direct_referrer_name"]),
                        _display(row["indirect_referrer_name"]),
                        _date_time(row["last_seen_at"]),
                    ]
                )
        elif tab == "attachments":
            writer.writerow(["附件ID", "文件名", "类型", "URL哈希", "备注", "上传时间", "最后采集"])
            rows = conn.execute(
                """
                SELECT source_file_id, file_name, content_type, file_url_hash, note, uploaded_at, last_seen_at
                FROM member_attachments
                WHERE member_id = ?
                ORDER BY COALESCE(uploaded_at, last_seen_at) DESC, id DESC
                LIMIT ?
                """,
                (member_id, MAX_EXPORT_ROWS),
            ).fetchall()
            for row in rows:
                writer.writerow(
                    [
                        _display(row["source_file_id"]),
                        _display(row["file_name"]),
                        _display(row["content_type"]),
                        _display(row["file_url_hash"]),
                        _display(row["note"]),
                        _date_time(row["uploaded_at"]),
                        _date_time(row["last_seen_at"]),
                    ]
                )
        else:
            account_where = "member_id = ?"
            account_args: list[Any] = [member_id]
            if account_scope:
                account_where += " AND item_scope = ?"
                account_args.append(account_scope)
            writer.writerow(["范围", "编号", "名称", "类型", "状态", "来源", "余额", "次数", "有效期起", "有效期至", "最后采集"])
            rows = conn.execute(
                """
                SELECT item_scope, item_no, item_name, item_type, status, source_name,
                       balance_cents, display_balance_text, remaining_times_text,
                       valid_from, valid_to, last_seen_at
                FROM member_account_items
                WHERE """ + account_where + """
                ORDER BY item_scope, COALESCE(valid_to, last_seen_at) DESC, id DESC
                LIMIT ?
                """,
                [*account_args, MAX_EXPORT_ROWS],
            ).fetchall()
            for row in rows:
                writer.writerow(
                    [
                        _item_scope_label(row["item_scope"]),
                        _display(row["item_no"]),
                        _display(row["item_name"]),
                        _display(row["item_type"]),
                        _display(row["status"]),
                        _display(row["source_name"]),
                        _display(row["display_balance_text"]) if row["display_balance_text"] else _money(row["balance_cents"]),
                        _display(row["remaining_times_text"]),
                        _date_only(row["valid_from"]),
                        _date_only(row["valid_to"]),
                        _date_time(row["last_seen_at"]),
                    ]
                )

    filename = f"mei1_member_{member_id}_{tab}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return output.getvalue().encode("utf-8"), filename, HTTPStatus.OK


def _csv_error(message: str) -> bytes:
    output = io.StringIO(newline="")
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(["错误"])
    writer.writerow([message])
    return output.getvalue().encode("utf-8")


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(db_path), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _parse_filters(params: dict[str, list[str]]) -> CustomerFilters:
    return CustomerFilters(
        name=_first_param(params, "name"),
        member_no=_first_param(params, "member_no"),
        mobile=_first_param(params, "mobile"),
        store=_first_param(params, "store"),
        grade=_first_param(params, "grade"),
        joined_from=_date_param(params, "joined_from"),
        joined_to=_date_param(params, "joined_to"),
        seen_from=_date_param(params, "seen_from"),
        seen_to=_date_param(params, "seen_to"),
        page=max(1, _int_param(params, "page", 1)),
        page_size=_page_size(_int_param(params, "page_size", DEFAULT_PAGE_SIZE)),
    )


def _where_clause(filters: CustomerFilters) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    args: list[Any] = []
    if filters.name:
        clauses.append("m.name LIKE ?")
        args.append(f"%{filters.name}%")
    if filters.member_no:
        clauses.append("m.member_no LIKE ?")
        args.append(f"%{filters.member_no}%")
    if filters.mobile:
        clauses.append("m.mobile_masked LIKE ?")
        args.append(f"%{filters.mobile}%")
    if filters.store:
        clauses.append("m.store_name = ?")
        args.append(filters.store)
    if filters.grade:
        clauses.append("m.grade_name = ?")
        args.append(filters.grade)
    if filters.joined_from:
        clauses.append("substr(m.joined_at, 1, 10) >= ?")
        args.append(filters.joined_from)
    if filters.joined_to:
        clauses.append("substr(m.joined_at, 1, 10) <= ?")
        args.append(filters.joined_to)
    if filters.seen_from:
        clauses.append("substr(m.last_seen_at, 1, 10) >= ?")
        args.append(filters.seen_from)
    if filters.seen_to:
        clauses.append("substr(m.last_seen_at, 1, 10) <= ?")
        args.append(filters.seen_to)
    where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
    return where_sql, args


def _load_members(
    conn: sqlite3.Connection,
    filters: CustomerFilters,
) -> tuple[list[sqlite3.Row], int, CustomerFilters]:
    where_sql, args = _where_clause(filters)
    total = int(conn.execute(f"SELECT COUNT(*) FROM members m {where_sql}", args).fetchone()[0])
    page_count = max(1, math.ceil(total / filters.page_size))
    current_page = min(filters.page, page_count)
    offset = (current_page - 1) * filters.page_size
    sql = f"""
        SELECT
          m.id, m.source_member_id, m.member_no, m.name, m.gender, m.mobile_masked,
          m.grade_name, m.member_layer, m.store_name, m.tracking_employee_name,
          m.exclusive_advisor_name, m.joined_at, m.last_seen_at, m.detail_last_seen_at,
          (SELECT COUNT(*) FROM member_account_items i WHERE i.member_id = m.id) AS account_count
        FROM members m
        {where_sql}
        ORDER BY COALESCE(m.last_seen_at, m.first_seen_at) DESC, m.id DESC
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, [*args, filters.page_size, offset]).fetchall()
    return rows, total, replace(filters, page=current_page)


def _load_stats(conn: sqlite3.Connection, db_path: Path) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS member_count,
          SUM(CASE WHEN detail_last_seen_at IS NOT NULL THEN 1 ELSE 0 END) AS detail_seen_count,
          COUNT(DISTINCT NULLIF(store_name, '')) AS store_count,
          MAX(last_seen_at) AS last_seen_at
        FROM members
        """
    ).fetchone()
    return {
        "db_name": db_path.name,
        "db_size": _format_bytes(db_path.stat().st_size),
        "member_count": int(row["member_count"] or 0),
        "detail_seen_count": int(row["detail_seen_count"] or 0),
        "store_count": int(row["store_count"] or 0),
        "last_seen_at": row["last_seen_at"],
    }


def _load_distinct_values(conn: sqlite3.Connection, column: str) -> list[str]:
    if column not in {"store_name", "grade_name"}:
        raise ValueError(f"Unsupported filter column: {column}")
    rows = conn.execute(
        f"""
        SELECT DISTINCT {column} AS value
        FROM members
        WHERE {column} IS NOT NULL AND {column} <> ''
        ORDER BY {column}
        LIMIT 200
        """
    ).fetchall()
    return [str(row["value"]) for row in rows]


def _render_filter_form(filters: CustomerFilters, stores: list[str], grades: list[str]) -> str:
    return f"""
    <form class="filters" action="/members" method="get">
      <label><span>姓名</span><input name="name" value="{_h(filters.name)}" placeholder="输入姓名"></label>
      <label><span>会员号</span><input name="member_no" value="{_h(filters.member_no)}" placeholder="输入会员号"></label>
      <label><span>手机号</span><input name="mobile" value="{_h(filters.mobile)}" placeholder="输入掩码片段"></label>
      <label><span>门店</span>{_render_select("store", filters.store, stores, "全部门店")}</label>
      <label><span>等级</span>{_render_select("grade", filters.grade, grades, "全部等级")}</label>
      <label><span>注册日期</span><input type="date" name="joined_from" value="{_h(filters.joined_from)}"></label>
      <label><span>至</span><input type="date" name="joined_to" value="{_h(filters.joined_to)}"></label>
      <label><span>采集日期</span><input type="date" name="seen_from" value="{_h(filters.seen_from)}"></label>
      <label><span>至</span><input type="date" name="seen_to" value="{_h(filters.seen_to)}"></label>
      <label><span>每页</span>{_render_page_size_select(filters.page_size)}</label>
      <div class="filter-actions">
        <button class="primary" type="submit">查询</button>
        <a class="button" href="/members">重置</a>
      </div>
    </form>
    """


def _render_select(name: str, selected: str, values: list[str], empty_label: str) -> str:
    options = [f'<option value="">{_h(empty_label)}</option>']
    for value in values:
        attr = " selected" if value == selected else ""
        options.append(f'<option value="{_h(value)}"{attr}>{_h(value)}</option>')
    return f'<select name="{_h(name)}">{"".join(options)}</select>'


def _render_page_size_select(selected: int) -> str:
    options = []
    for value in PAGE_SIZE_OPTIONS:
        attr = " selected" if value == selected else ""
        options.append(f'<option value="{value}"{attr}>{value}条</option>')
    return f'<select name="page_size">{"".join(options)}</select>'


def _render_members_table(rows: list[sqlite3.Row], filters: CustomerFilters) -> str:
    if not rows:
        return _render_empty("没有匹配的客户")
    return f"""
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>会员号</th>
            <th>姓名</th>
            <th>手机号</th>
            <th>等级</th>
            <th>门店</th>
            <th>注册日期</th>
            <th>最后采集</th>
            <th>详情</th>
            <th>账户</th>
          </tr>
        </thead>
        <tbody>
          {"".join(_render_member_row(row, filters) for row in rows)}
        </tbody>
      </table>
    </div>
    """


def _render_member_row(row: sqlite3.Row, filters: CustomerFilters) -> str:
    list_query = _query_string(filters)
    return_to = "/members" + (f"?{list_query}" if list_query else "")
    detail_href = f"/members/{row['id']}?{urlencode({'return': return_to})}"
    return f"""
      <tr>
        <td class="mono">{_h(row["member_no"] or "-")}</td>
        <td><strong>{_h(row["name"] or "未命名")}</strong><div class="subtle">{_h(_gender_label(row["gender"]))}</div></td>
        <td class="mono">{_h(row["mobile_masked"] or "-")}</td>
        <td>{_badge(row["grade_name"] or "未分级", "grade")}</td>
        <td class="clip">{_h(row["store_name"] or "-")}</td>
        <td>{_h(_date_only(row["joined_at"]))}</td>
        <td>{_h(_date_time(row["last_seen_at"]))}</td>
        <td><a class="link-action" href="{_h(detail_href)}">查看</a><div>{_badge("已采" if row["detail_last_seen_at"] else "未采", "ok" if row["detail_last_seen_at"] else "warn")}</div></td>
        <td>{int(row["account_count"] or 0)}</td>
      </tr>
    """


def _render_pagination(filters: CustomerFilters, total: int) -> str:
    page_count = max(1, math.ceil(total / filters.page_size))
    start = 0 if total == 0 else (filters.page - 1) * filters.page_size + 1
    end = min(total, filters.page * filters.page_size)
    pages = _page_window(filters.page, page_count)
    links = [
        _page_link("首页", 1, filters, disabled=filters.page <= 1),
        _page_link("上一页", filters.page - 1, filters, disabled=filters.page <= 1),
    ]
    for page in pages:
        links.append(_page_link(str(page), page, filters, current=page == filters.page))
    links.extend(
        [
            _page_link("下一页", filters.page + 1, filters, disabled=filters.page >= page_count),
            _page_link("末页", page_count, filters, disabled=filters.page >= page_count),
        ]
    )
    return f"""
    <div class="pagination">
      <span>显示 {start}-{end} / {total}</span>
      <nav>{"".join(links)}</nav>
    </div>
    """


def _page_window(current: int, page_count: int) -> list[int]:
    if page_count <= 7:
        return list(range(1, page_count + 1))
    start = max(1, current - 2)
    end = min(page_count, current + 2)
    if start == 1:
        end = min(page_count, 5)
    if end == page_count:
        start = max(1, page_count - 4)
    pages = list(range(start, end + 1))
    if 1 not in pages:
        pages.insert(0, 1)
    if page_count not in pages:
        pages.append(page_count)
    return pages


def _page_link(
    label: str,
    page: int,
    filters: CustomerFilters,
    *,
    disabled: bool = False,
    current: bool = False,
) -> str:
    classes = "page-link"
    if current:
        classes += " current"
    if disabled:
        return f'<span class="{classes} disabled">{_h(label)}</span>'
    query = _query_string(filters, overrides={"page": page})
    return f'<a class="{classes}" href="/members?{_h(query)}">{_h(label)}</a>'


def _member_basic_pairs(member: sqlite3.Row) -> tuple[tuple[str, Any], ...]:
    return (
        ("姓名", member["name"]),
        ("性别", _gender_label(member["gender"])),
        ("手机号", member["mobile_masked"]),
        ("微信号", member["wechat_account"]),
        ("微信绑定", _bool_label(member["wechat_bound"])),
        ("会员等级", member["grade_name"]),
        ("客户分层", member["member_layer"]),
        ("来源渠道", member["source_channel"]),
        ("门店", member["store_name"]),
        ("跟踪员工", member["tracking_employee_name"]),
        ("专属顾问", member["exclusive_advisor_name"]),
        ("推荐人", member["referrer_name"]),
        ("职业", member["occupation"]),
        ("生日", member["birthday_date"]),
        ("下次生日", member["next_birthday_date"]),
        ("年龄", member["age"]),
        ("地址", member["address"]),
        ("注册日期", _date_time(member["joined_at"])),
        ("备注", member["note"]),
    )


def _render_key_values(pairs: tuple[tuple[str, Any], ...]) -> str:
    return "<dl class=\"kv-grid\">" + "".join(
        f"<div><dt>{_h(label)}</dt><dd>{_h(_display(value))}</dd></div>" for label, value in pairs
    ) + "</dl>"


def _render_account_items(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return _render_empty("暂无账户项目")
    body = []
    for row in rows:
        balance = row["display_balance_text"] or _money(row["balance_cents"]) or "-"
        body.append(
            f"""
            <tr>
              <td>{_h(_item_scope_label(row["item_scope"]))}</td>
              <td>{_h(row["item_name"] or row["item_no"] or "-")}</td>
              <td>{_h(row["item_type"] or "-")}</td>
              <td>{_h(row["status"] or "-")}</td>
              <td>{_h(row["source_name"] or "-")}</td>
              <td>{_h(balance)}</td>
              <td>{_h(row["remaining_times_text"] or "-")}</td>
              <td>{_h(_date_only(row["valid_to"]))}</td>
            </tr>
            """
        )
    return f"""
    <div class="table-wrap small">
      <table>
        <thead><tr><th>范围</th><th>名称</th><th>类型</th><th>状态</th><th>来源</th><th>余额</th><th>次数</th><th>有效期至</th></tr></thead>
        <tbody>{"".join(body)}</tbody>
      </table>
    </div>
    """


def _render_asset_snapshots(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return _render_empty("暂无资产快照")
    body = []
    for row in rows:
        body.append(
            f"""
            <tr>
              <td>{_h(_date_time(row["observed_at"]))}</td>
              <td>{_h(_money(row["member_wallet_cents"]))}</td>
              <td>{_h(_money(row["remaining_consume_value_cents"]))}</td>
              <td>{_h(row["points"] if row["points"] is not None else "-")}</td>
              <td>{_h(_money(row["debt_cents"]))}</td>
              <td>{_h(row["card_count"] if row["card_count"] is not None else "-")}</td>
              <td>{_h(row["total_visit_count"] if row["total_visit_count"] is not None else "-")}</td>
              <td>{_h(row["lifecycle_category"] or "-")}</td>
            </tr>
            """
        )
    return f"""
    <div class="table-wrap small">
      <table>
        <thead><tr><th>时间</th><th>钱包</th><th>剩余消费</th><th>积分</th><th>欠款</th><th>卡数</th><th>到店</th><th>生命周期</th></tr></thead>
        <tbody>{"".join(body)}</tbody>
      </table>
    </div>
    """


def _render_service_records(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return _render_empty("暂无服务记录")
    body = []
    for row in rows:
        body.append(
            f"""
            <tr>
              <td>{_h(_record_type_label(row["record_type"]))}</td>
              <td>{_h(_date_time(row["record_at"]))}</td>
              <td>{_h(row["employee_name"] or "-")}</td>
              <td>{_h(row["content"] or "-")}</td>
              <td>{_h(row["related_items_text"] or "-")}</td>
            </tr>
            """
        )
    return f"""
    <div class="table-wrap small">
      <table>
        <thead><tr><th>类型</th><th>时间</th><th>员工</th><th>内容</th><th>关联项目</th></tr></thead>
        <tbody>{"".join(body)}</tbody>
      </table>
    </div>
    """


def _render_detail_records(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return _render_empty("暂无客户数据明细")
    body = []
    for row in rows:
        details = " / ".join(
            part
            for part in (
                row["store_name"],
                row["room_name"],
                row["employee_name"],
            )
            if part
        )
        body.append(
            f"""
            <tr>
              <td>{_h(_detail_category_label(row["category"]))}</td>
              <td>{_h(_date_time(row["happened_at"]))}</td>
              <td>{_h(row["title"] or row["order_no"] or "-")}</td>
              <td>{_h(details or "-")}</td>
              <td>{_h(_money(row["amount_cents"]))}</td>
              <td>{_h(row["duration_minutes"] if row["duration_minutes"] is not None else "-")}</td>
              <td>{_h(row["status"] or "-")}</td>
              <td class="text-cell">{_h(row["content"] or "-")}</td>
            </tr>
            """
        )
    return f"""
    <div class="table-wrap small">
      <table>
        <thead><tr><th>分类</th><th>时间</th><th>标题/单号</th><th>门店/房间/员工</th><th>金额</th><th>时长</th><th>状态</th><th>内容</th></tr></thead>
        <tbody>{"".join(body)}</tbody>
      </table>
    </div>
    """


def _render_survey_profiles(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return _render_empty("暂无美问问卷")
    body = []
    for row in rows:
        body.append(
            f"""
            <tr>
              <td class="mono">{_h(row["source_profile_id"] or "-")}</td>
              <td>{_h(row["profile_name"] or "-")}</td>
              <td class="text-cell">{_h(row["profile_url"] or "-")}</td>
              <td class="text-cell">{_h(_json_value_summary(row["field_values_json"]))}</td>
              <td>{_h(_date_time(row["last_seen_at"]))}</td>
            </tr>
            """
        )
    return f"""
    <div class="table-wrap small">
      <table>
        <thead><tr><th>问卷ID</th><th>名称</th><th>地址</th><th>字段摘要</th><th>最后采集</th></tr></thead>
        <tbody>{"".join(body)}</tbody>
      </table>
    </div>
    """


def _render_partner_infos(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return _render_empty("暂无合伙人信息")
    body = []
    for row in rows:
        body.append(
            f"""
            <tr>
              <td class="mono">{_h(row["partner_member_id"] or "-")}</td>
              <td>{_h(row["partner_level"] or "-")}</td>
              <td>{_h(_money(row["store_balance_cents"]))}</td>
              <td>{_h(_money(row["withdrawable_cents"]))}</td>
              <td>{_h(row["direct_referrer_name"] or "-")}</td>
              <td>{_h(row["indirect_referrer_name"] or "-")}</td>
              <td>{_h(_date_time(row["last_seen_at"]))}</td>
            </tr>
            """
        )
    return f"""
    <div class="table-wrap small">
      <table>
        <thead><tr><th>合伙人ID</th><th>等级</th><th>店铺余额</th><th>可提现</th><th>直接推荐人</th><th>间接推荐人</th><th>最后采集</th></tr></thead>
        <tbody>{"".join(body)}</tbody>
      </table>
    </div>
    """


def _render_attachments(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return _render_empty("暂无客户附件")
    body = []
    for row in rows:
        body.append(
            f"""
            <tr>
              <td class="mono">{_h(row["source_file_id"] or "-")}</td>
              <td>{_h(row["file_name"] or "-")}</td>
              <td>{_h(row["content_type"] or "-")}</td>
              <td class="mono">{_h(row["file_url_hash"] or "-")}</td>
              <td class="text-cell">{_h(row["note"] or "-")}</td>
              <td>{_h(_date_time(row["uploaded_at"]))}</td>
              <td>{_h(_date_time(row["last_seen_at"]))}</td>
            </tr>
            """
        )
    return f"""
    <div class="table-wrap small">
      <table>
        <thead><tr><th>附件ID</th><th>文件名</th><th>类型</th><th>URL哈希</th><th>备注</th><th>上传时间</th><th>最后采集</th></tr></thead>
        <tbody>{"".join(body)}</tbody>
      </table>
    </div>
    """


def _render_tags(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return '<div class="tag-row"><span class="muted-line">暂无标签</span></div>'
    tags = "".join(f'<span class="tag">{_h(row["tag_name"])}</span>' for row in rows)
    return f'<div class="tag-row">{tags}</div>'


def _render_empty(message: str) -> str:
    return f'<div class="empty">{_h(message)}</div>'


def _render_not_found(message: str) -> str:
    return _layout("未找到", f"<section class=\"panel\">{_render_empty(message)}</section>", None)


def _layout(title: str, content: str, stats: dict[str, Any] | None) -> str:
    db_meta = _render_db_meta(stats)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_h(title)} - Mei1</title>
  <style>{STYLE}</style>
</head>
<body>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand">Mei1</div>
      <nav class="side-nav" aria-label="主导航">
        <a class="active" href="/members">客户浏览</a>
      </nav>
      {db_meta}
    </aside>
    <main class="main">
      {content}
    </main>
  </div>
</body>
</html>"""


def _render_db_meta(stats: dict[str, Any] | None) -> str:
    if not stats:
        return ""
    return f"""
    <section class="db-meta">
      <h2>数据库信息</h2>
      <dl>
        <div><dt>文件</dt><dd>{_h(stats["db_name"])}</dd></div>
        <div><dt>大小</dt><dd>{_h(stats["db_size"])}</dd></div>
        <div><dt>门店</dt><dd>{stats["store_count"]}</dd></div>
        <div><dt>最后采集</dt><dd>{_h(_date_time(stats["last_seen_at"]))}</dd></div>
      </dl>
      <p class="health">只读连接正常</p>
    </section>
    """


def _query_string(filters: CustomerFilters, overrides: dict[str, Any] | None = None) -> str:
    values = asdict(filters)
    values.update(overrides or {})
    compact = {
        key: str(value)
        for key, value in values.items()
        if value not in ("", None) and not (key == "page" and int(value) <= 1)
    }
    return urlencode(compact)


def _member_detail_href(member_id: int, return_to: str, tab: str, **overrides: Any) -> str:
    values: dict[str, Any] = {"tab": tab}
    if return_to:
        values["return"] = return_to
    values.update(overrides)
    compact = {}
    for key, value in values.items():
        if value in ("", None):
            continue
        if key.endswith("_page"):
            try:
                if int(value) <= 1:
                    continue
            except (TypeError, ValueError):
                continue
        compact[key] = str(value)
    return f"/members/{member_id}?{urlencode(compact)}"


def _member_export_href(member_id: int, tab: str, params: dict[str, list[str]]) -> str:
    values: dict[str, Any] = {"member_id": member_id, "tab": tab}
    for key in ("account_scope", "service_type", "detail_category"):
        value = _first_param(params, key)
        if value:
            values[key] = value
    return f"/member-export.csv?{urlencode(values)}"


def _parse_member_id(path: str) -> int | None:
    value = path.removeprefix("/members/").strip("/")
    if not value.isdigit():
        return None
    return int(value)


def _safe_return_url(value: str) -> str | None:
    if not value:
        return None
    if value.startswith("/members") and not value.startswith("//"):
        return value
    return None


def _detail_tab_from_params(params: dict[str, list[str]]) -> str:
    value = _first_param(params, "tab")
    valid = {tab for tab, _label in DETAIL_TABS}
    return value if value in valid else "account"


def _scoped_param(params: dict[str, list[str]], name: str, allowed: tuple[str, ...]) -> str:
    value = _first_param(params, name)
    return value if value in allowed else ""


def _count_rows(conn: sqlite3.Connection, sql: str, args: list[Any] | tuple[Any, ...]) -> int:
    return int(conn.execute(sql, args).fetchone()[0] or 0)


def _bounded_page(page: int, total: int, page_size: int) -> int:
    page_count = max(1, math.ceil(total / page_size))
    return min(max(1, page), page_count)


def _group_counts(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...]) -> dict[str, int]:
    rows = conn.execute(sql, args).fetchall()
    return {str(row["key"]): int(row["count"] or 0) for row in rows if row["key"] is not None}


def _ordered_count_keys(counts: dict[str, int], order: tuple[str, ...]) -> list[str]:
    ordered = list(order)
    extras = sorted(key for key in counts if key not in ordered)
    return ordered + extras


def _raw_json_summary(raw: str | None) -> str:
    if not raw:
        return "无原始资料 JSON"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return "原始资料 JSON 无法解析"
    if isinstance(data, dict):
        keys = sorted(str(key) for key in data.keys())
        preview = ", ".join(keys[:30])
        suffix = "" if len(keys) <= 30 else f" ... 共 {len(keys)} 个字段"
        return f"顶层字段: {preview}{suffix}"
    if isinstance(data, list):
        return f"数组资料: {len(data)} 条"
    return f"资料类型: {type(data).__name__}"


def _json_value_summary(raw: str | None) -> str:
    if not raw:
        return "-"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return str(raw)[:500]
    if isinstance(data, dict):
        parts = []
        for key, value in list(data.items())[:12]:
            if isinstance(value, dict):
                display = f"对象({len(value)})"
            elif isinstance(value, list):
                display = f"数组({len(value)})"
            else:
                display = _display(value)
            parts.append(f"{key}: {display}")
        suffix = "" if len(data) <= 12 else f" ... 共 {len(data)} 字段"
        return "；".join(parts) + suffix
    if isinstance(data, list):
        return f"数组({len(data)})"
    return _display(data)


def _badge(text: str, kind: str) -> str:
    return f'<span class="badge {kind}">{_h(text)}</span>'


def _first_param(params: dict[str, list[str]], name: str) -> str:
    value = params.get(name, [""])[0]
    return str(value).strip()


def _date_param(params: dict[str, list[str]], name: str) -> str:
    value = _first_param(params, name)
    if not value:
        return ""
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return ""
    return value


def _int_param(params: dict[str, list[str]], name: str, default: int) -> int:
    try:
        return int(_first_param(params, name))
    except ValueError:
        return default


def _page_size(value: int) -> int:
    if value in PAGE_SIZE_OPTIONS:
        return value
    return DEFAULT_PAGE_SIZE


def _display(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


def _csv_value(value: Any, column: str) -> str:
    if column == "gender":
        return _gender_label(value)
    if column == "wechat_bound":
        return _bool_label(value)
    return "" if value is None else str(value)


def _gender_label(value: Any) -> str:
    if value == "male":
        return "男"
    if value == "female":
        return "女"
    return _display(value)


def _bool_label(value: Any) -> str:
    if value == 1:
        return "是"
    if value == 0:
        return "否"
    return "-"


def _date_only(value: Any) -> str:
    text = _display(value)
    return text[:10] if text != "-" else text


def _date_time(value: Any) -> str:
    text = _display(value)
    if text == "-":
        return text
    return text.replace("T", " ").replace("Z", "")[:19]


def _money(cents: Any) -> str:
    if cents is None or cents == "":
        return "-"
    try:
        return f"¥{int(cents) / 100:,.2f}"
    except (TypeError, ValueError):
        return str(cents)


def _format_bytes(value: int) -> str:
    units = ("B", "KB", "MB", "GB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{value} B"


def _initials(value: Any) -> str:
    text = _display(value)
    if text == "-":
        return "?"
    return text[:2]


def _item_scope_label(value: Any) -> str:
    labels = {
        "held_card": "持有卡",
        "coupon": "优惠券",
        "present": "赠品",
        "mall_coupon": "商城券",
        "transferred": "转赠",
        "deposit_item": "寄存",
    }
    return labels.get(str(value), _display(value))


def _record_type_label(value: Any) -> str:
    labels = {
        "return_visit": "回访",
        "service_note": "服务备注",
        "development_plan": "开发计划",
        "other": "其他",
    }
    return labels.get(str(value), _display(value))


def _detail_category_label(value: Any) -> str:
    labels = {
        "appointment": "预约",
        "consume": "消费",
        "gift": "礼品",
        "points": "积分",
        "modification": "修改",
        "mall_order": "商城订单",
        "face_scan": "刷脸",
        "reach_store": "到店",
        "wallet": "钱包",
        "deposit": "寄存",
        "other": "其他",
    }
    return labels.get(str(value), _display(value))


def _h(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


STYLE = """
:root {
  color-scheme: light;
  --bg: #f5f7f6;
  --panel: #ffffff;
  --line: #dfe5e2;
  --text: #17211b;
  --muted: #68736d;
  --green: #147a35;
  --green-soft: #e6f4ea;
  --amber: #b66a05;
  --amber-soft: #fff4df;
  --blue: #245a8d;
  --blue-soft: #e8f0f8;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
a { color: inherit; text-decoration: none; }
.app-shell {
  min-height: 100vh;
  display: grid;
  grid-template-columns: 216px minmax(0, 1fr);
}
.sidebar {
  position: sticky;
  top: 0;
  height: 100vh;
  padding: 24px 14px;
  background: #fbfcfb;
  border-right: 1px solid var(--line);
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.brand {
  padding: 0 10px 10px;
  font-size: 28px;
  font-weight: 800;
  color: var(--green);
}
.side-nav a {
  display: block;
  padding: 11px 12px;
  border-radius: 8px;
  color: #25342b;
  font-weight: 650;
}
.side-nav a.active {
  background: var(--green-soft);
  color: var(--green);
  border-left: 4px solid var(--green);
}
.db-meta {
  margin-top: auto;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
}
.db-meta h2 {
  margin: 0 0 10px;
  font-size: 13px;
}
.db-meta dl,
.kv-grid {
  margin: 0;
}
.db-meta div,
.kv-grid div {
  display: grid;
  grid-template-columns: 86px minmax(0, 1fr);
  gap: 8px;
  padding: 5px 0;
}
dt {
  color: var(--muted);
}
dd {
  margin: 0;
  min-width: 0;
  overflow-wrap: anywhere;
}
.db-meta dd {
  overflow-wrap: normal;
}
.health {
  margin: 12px 0 0;
  color: var(--green);
  font-weight: 650;
}
.main {
  padding: 24px;
  min-width: 0;
}
.page-head,
.detail-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.page-head h1,
.detail-head h1 {
  margin: 0;
  font-size: 24px;
  letter-spacing: 0;
}
.eyebrow {
  margin: 0 0 4px;
  color: var(--muted);
  font-size: 12px;
}
.button,
button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 36px;
  padding: 0 14px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--panel);
  color: var(--text);
  font-weight: 650;
  cursor: pointer;
}
button.primary,
.button.export {
  border-color: var(--green);
  background: var(--green);
  color: white;
}
.filters {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(136px, 1fr));
  gap: 12px;
  align-items: end;
  padding: 16px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}
label span {
  display: block;
  margin-bottom: 6px;
  color: var(--muted);
  font-size: 12px;
}
label,
input,
select {
  min-width: 0;
}
input,
select {
  width: 100%;
  min-height: 36px;
  padding: 0 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: white;
  color: var(--text);
  font: inherit;
}
.filter-actions {
  display: flex;
  gap: 8px;
  align-self: end;
}
.summary-strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin: 16px 0;
}
.summary-strip div,
.metric-grid div {
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
}
.summary-strip strong,
.metric-grid strong {
  display: block;
  font-size: 20px;
}
.summary-strip span,
.metric-grid span {
  color: var(--muted);
}
.data-panel,
.panel {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
}
.panel {
  padding: 16px;
  margin-bottom: 16px;
}
.panel h2 {
  margin: 0 0 12px;
  font-size: 16px;
}
.panel h3 {
  margin: 18px 0 10px;
  font-size: 14px;
}
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.section-head h2 {
  margin: 0;
}
.tab-nav {
  display: flex;
  gap: 8px;
  margin: 0 0 16px;
  overflow-x: auto;
  border-bottom: 1px solid var(--line);
}
.tab-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 42px;
  padding: 0 12px;
  border: 1px solid var(--line);
  border-bottom: 0;
  border-radius: 8px 8px 0 0;
  background: #f8faf9;
  color: #4f5b55;
  white-space: nowrap;
}
.tab-link.active {
  background: var(--panel);
  color: var(--green);
  border-top: 3px solid var(--green);
  font-weight: 750;
}
.scope-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 0 0 12px;
}
.scope-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 32px;
  padding: 0 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: white;
  color: #4f5b55;
}
.scope-chip.active {
  border-color: var(--green);
  background: var(--green-soft);
  color: var(--green);
  font-weight: 750;
}
.pill-count {
  min-width: 20px;
  padding: 1px 6px;
  border-radius: 999px;
  background: #edf1ef;
  color: #53605a;
  font-size: 12px;
  text-align: center;
}
.table-wrap {
  width: 100%;
  overflow-x: auto;
}
table {
  width: 100%;
  border-collapse: collapse;
  min-width: 900px;
}
.small table {
  min-width: 760px;
}
th,
td {
  padding: 12px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: middle;
}
th {
  color: #4f5b55;
  font-size: 12px;
  font-weight: 750;
  background: #fbfcfb;
}
tbody tr:hover {
  background: #f8fbf9;
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.subtle,
.muted-line {
  color: var(--muted);
  font-size: 12px;
}
.clip {
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.badge,
.tag {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  white-space: nowrap;
}
.badge.grade {
  color: var(--blue);
  background: var(--blue-soft);
}
.badge.ok {
  color: var(--green);
  background: var(--green-soft);
}
.badge.warn {
  color: var(--amber);
  background: var(--amber-soft);
}
.link-action {
  color: var(--green);
  font-weight: 750;
}
.pagination {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 14px;
}
.pagination nav {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.page-link {
  min-width: 34px;
  padding: 7px 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  text-align: center;
  background: white;
}
.page-link.current {
  border-color: var(--green);
  background: var(--green);
  color: white;
}
.page-link.disabled {
  color: #a0aaa4;
  background: #f2f4f3;
}
.identity {
  display: flex;
  align-items: center;
  gap: 14px;
  min-width: 0;
}
.avatar {
  flex: 0 0 auto;
  display: grid;
  place-items: center;
  width: 56px;
  height: 56px;
  border-radius: 50%;
  background: var(--green-soft);
  color: var(--green);
  font-weight: 800;
}
.status-stack {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}
.detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(260px, 1fr);
  gap: 16px;
}
.detail-grid .wide {
  grid-row: span 2;
}
.metric-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}
.metric-grid.compact {
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin-bottom: 10px;
}
.table-meta,
.table-pager {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 12px 0 0;
  color: var(--muted);
}
.table-pager nav {
  display: flex;
  gap: 6px;
}
.text-cell {
  max-width: 360px;
  overflow-wrap: anywhere;
}
.tag-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}
.tag {
  color: var(--green);
  background: var(--green-soft);
}
.raw-summary,
.empty {
  padding: 14px;
  border: 1px dashed var(--line);
  border-radius: 8px;
  color: var(--muted);
  background: #fbfcfb;
  overflow-wrap: anywhere;
}
@media (max-width: 1100px) {
  .app-shell {
    grid-template-columns: 1fr;
  }
  .sidebar {
    position: static;
    height: auto;
    flex-direction: row;
    align-items: center;
    justify-content: space-between;
  }
  .db-meta {
    display: none;
  }
  .filters {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .summary-strip,
  .detail-grid,
  .metric-grid.compact {
    grid-template-columns: 1fr;
  }
}
@media (max-width: 640px) {
  .main {
    padding: 16px;
  }
  .page-head,
  .detail-head,
  .pagination {
    align-items: stretch;
    flex-direction: column;
  }
  .filters {
    grid-template-columns: 1fr;
  }
  .filter-actions {
    flex-direction: column;
  }
  .button,
  button {
    width: 100%;
  }
}
"""
