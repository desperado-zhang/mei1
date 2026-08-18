from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class EgoBrowserError(RuntimeError):
    pass


_DETAIL_CAPTURE_HELPERS_JS = r"""
    const getOptionalService = (name) => {
      try { return injector.get(name); } catch (e) { return null; }
    };
    const apiCard = getOptionalService('api.card');
    const apiMarketing = getOptionalService('api.marketing');
    const detailState = { memberOverviewPermissionDenied: false };
    const pageSizeForDetails = 100;
    const toSourceId = (value) => String(value);
    const toRequestId = (value) => /^\d+$/.test(String(value)) ? Number(value) : value;
    const rowsFrom = (payload) => {
      const data = payload && Object.prototype.hasOwnProperty.call(payload, 'data') ? payload.data : payload;
      const walk = (value) => {
        if (Array.isArray(value)) return value.filter((item) => item && typeof item === 'object');
        if (!value || typeof value !== 'object') return [];
        for (const key of ['rows', 'list', 'records', 'items', 'data']) {
          const child = value[key];
          const rows = walk(child);
          if (rows.length) return rows;
        }
        return [];
      };
      return walk(data);
    };
    const totalFrom = (payload) => {
      const stack = [payload];
      while (stack.length) {
        const value = stack.pop();
        if (!value || typeof value !== 'object') continue;
        for (const key of ['total', 'totalCount', 'count', 'recordsTotal']) {
          const raw = value[key];
          if (Number.isFinite(raw)) return Number(raw);
          if (typeof raw === 'string' && /^\d+$/.test(raw)) return Number(raw);
        }
        for (const child of Object.values(value)) {
          if (child && typeof child === 'object') stack.push(child);
        }
      }
      return null;
    };
    const pushPayload = (method, endpoint, requestUrl, requestParams, requestBody, responseJson) => {
      payloads.push({
        method,
        endpoint,
        requestUrl,
        requestParams: requestParams || {},
        requestBody: requestBody === undefined ? null : requestBody,
        statusCode: 200,
        responseJson
      });
    };
    const callOnce = async (method, endpoint, requestUrl, requestParams, requestBody, fn, options = {}) => {
      try {
        const responseJson = await fn();
        pushPayload(method, endpoint, requestUrl, requestParams, requestBody, responseJson);
        return responseJson;
      } catch (e) {
        const error = serializeError(e);
        const permissionDenied = isPermissionError(error);
        errors.push({ endpoint, permissionDenied, error });
        if (options.markOverviewDenied && permissionDenied) {
          detailState.memberOverviewPermissionDenied = true;
        }
        return null;
      }
    };
    const callPaged = async ({ method = 'POST', endpoint, requestUrl, makeBody, fn, maxPages = 50 }) => {
      for (let page = 1; page <= maxPages; page++) {
        const requestBody = makeBody(page, pageSizeForDetails);
        let responseJson = null;
        try {
          responseJson = await fn(requestBody);
          pushPayload(method, endpoint, requestUrl, {}, requestBody, responseJson);
        } catch (e) {
          const error = serializeError(e);
          errors.push({ endpoint, permissionDenied: isPermissionError(error), error });
          break;
        }
        const rows = rowsFrom(responseJson);
        const total = totalFrom(responseJson);
        if (!total || rows.length === 0 || page * pageSizeForDetails >= total) break;
      }
    };
    const queryRequest = (memberId, type, page, size, includeStore = false) => ({
      page,
      size,
      query: [
        { field: 'merchantId', value: mch.mch_id },
        { field: 'memberId', value: toRequestId(memberId) },
        { field: 'type', value: String(type) },
        ...(includeStore ? [{ field: 'storeId', value: mch.store_id }] : [])
      ]
    });
    const accountRequest = (memberId, type, status, page, size) => ({
      page,
      size,
      query: [
        { field: 'merchantId', value: mch.mch_id },
        { field: 'memberId', value: toRequestId(memberId) },
        { field: 'type', value: String(type) },
        { field: 'status', value: status }
      ]
    });
    const merchantMemberQuery = (memberId, page, size) => ({
      page,
      size,
      query: [
        { field: 'merchantId', value: mch.mch_id },
        { field: 'memberId', value: toRequestId(memberId) }
      ]
    });
    const captureMemberDetailPayloads = async (memberId, includeAssetOverview) => {
      const memberRequestId = toRequestId(memberId);
      await callOnce('GET', '/api/member/detailInfo/' + memberId, 'ego-service://api.member.memberInfo/' + memberId, null, null, () => api.memberInfo(memberId));
      await callOnce('GET', '/api/member/detail/' + memberId, 'ego-service://api.member.memberDetailOfBooking/' + memberId, null, null, () => api.memberDetailOfBooking(memberId));
      if (typeof api.userInfo === 'function') {
        await callOnce('GET', '/api/member/' + memberId, 'ego-service://api.member.userInfo/' + memberId, null, null, () => api.userInfo(memberId));
      }
      if (typeof api.getMemberAttr === 'function') {
        await callOnce('GET', '/api/member/memberAttr/' + memberId, 'ego-service://api.member.getMemberAttr/' + memberId, null, null, () => api.getMemberAttr(memberId));
      }
      if (typeof api.memberUsreInfo === 'function') {
        await callOnce('GET', '/api/storePartnerAccount/queryByMemberId/' + memberId, 'ego-service://api.member.memberUsreInfo/' + memberId, null, null, () => api.memberUsreInfo(memberId));
      }
      if (typeof api.getMemberConsumeValue === 'function') {
        await callOnce('GET', '/api/member/queryMemberRemainConsumeValue/' + memberId, 'ego-service://api.member.getMemberConsumeValue/' + memberId, null, null, () => api.getMemberConsumeValue(memberId));
      }
      if (includeAssetOverview && !detailState.memberOverviewPermissionDenied) {
        await callOnce(
          'GET',
          '/api/member/amount/' + memberId,
          'ego-service://api.member.memberOverview/' + memberId,
          { storeIds },
          null,
          () => api.memberOverview(memberId, storeIds),
          { markOverviewDenied: true }
        );
      }

      await callPaged({
        endpoint: '/api/member/list/cardAndPresent',
        requestUrl: 'ego-service://api.member.cardList/held_card/' + memberId,
        makeBody: (page, size) => accountRequest(memberId, 1, 2, page, size),
        fn: (body) => api.cardList(body)
      });
      await callPaged({
        endpoint: '/api/member/list/cardAndPresent',
        requestUrl: 'ego-service://api.member.cardList/coupon/' + memberId,
        makeBody: (page, size) => accountRequest(memberId, 2, 1, page, size),
        fn: (body) => api.cardList(body)
      });
      await callPaged({
        endpoint: '/api/member/list/cardAndPresent',
        requestUrl: 'ego-service://api.member.cardList/present/' + memberId,
        makeBody: (page, size) => accountRequest(memberId, 3, 1, page, size),
        fn: (body) => api.cardList(body)
      });
      if (typeof api.memberCouponSearch === 'function') {
        await callPaged({
          endpoint: '/api/couponUser/memberCouponSearch',
          requestUrl: 'ego-service://api.member.memberCouponSearch/' + memberId,
          makeBody: (page, size) => merchantMemberQuery(memberId, page, size),
          fn: (body) => api.memberCouponSearch(body)
        });
      }
      if (apiCard && typeof apiCard.cardPresentsList === 'function') {
        await callPaged({
          endpoint: '/api/giveTradeRecord/giveFirendSearch',
          requestUrl: 'ego-service://api.card.cardPresentsList/' + memberId,
          makeBody: (page, size) => merchantMemberQuery(memberId, page, size),
          fn: (body) => apiCard.cardPresentsList(body)
        });
      }
      if (typeof api.getProductDepositList === 'function') {
        await callPaged({
          endpoint: '/api/deposit/depositStock/searchStockListData',
          requestUrl: 'ego-service://api.member.getProductDepositList/' + memberId,
          makeBody: (page, size) => ({ merchantId: mch.mch_id, memberId: memberRequestId, size, page }),
          fn: (body) => api.getProductDepositList(body)
        });
      }

      if (typeof api.memberServiceList === 'function') {
        await callPaged({
          endpoint: '/api/wechatbusinessassists/memberServiceList',
          requestUrl: 'ego-service://api.member.memberServiceList/' + memberId,
          makeBody: (page, size) => ({ merchantId: mch.mch_id, memberId: memberRequestId, page, rows: size, queryParams: {} }),
          fn: (body) => api.memberServiceList(body)
        });
      }

      for (const item of [
        [1, true],
        [2, false],
        [4, true],
        [5, true],
        [6, true]
      ]) {
        const [type, includeStore] = item;
        await callPaged({
          endpoint: '/api/member/list/record',
          requestUrl: 'ego-service://api.member.recordList/type-' + type + '/' + memberId,
          makeBody: (page, size) => queryRequest(memberId, type, page, size, includeStore),
          fn: (body) => api.recordList(body)
        });
      }
      if (typeof api.pointsChangeRecord === 'function') {
        await callPaged({
          endpoint: '/api/pointsChangeRecord/search',
          requestUrl: 'ego-service://api.member.pointsChangeRecord/' + memberId,
          makeBody: (page, size) => merchantMemberQuery(memberId, page, size),
          fn: (body) => api.pointsChangeRecord(body)
        });
      }
      if (typeof api.mallMemberTrade === 'function') {
        await callPaged({
          endpoint: '/api/mallItemTrade/mallMemberTrade',
          requestUrl: 'ego-service://api.member.mallMemberTrade/' + memberId,
          makeBody: (page, size) => ({ page, size, memberId: memberRequestId }),
          fn: (body) => api.mallMemberTrade(body)
        });
      }
      if (apiMarketing && typeof apiMarketing.faceBrushFaceRecord === 'function') {
        await callPaged({
          endpoint: '/api/dragonflyBrushFace/brushFaceRecord',
          requestUrl: 'ego-service://api.marketing.faceBrushFaceRecord/' + memberId,
          makeBody: (page, size) => ({ page, size, query: [{ field: 'holderId', value: memberRequestId }] }),
          fn: (body) => apiMarketing.faceBrushFaceRecord(body)
        });
      }
      if (typeof api.reachStoreRecord === 'function') {
        await callPaged({
          endpoint: '/api/member/reachStore/record',
          requestUrl: 'ego-service://api.member.reachStoreRecord/' + memberId,
          makeBody: (page, size) => ({ page, size, memberId: memberRequestId }),
          fn: (body) => api.reachStoreRecord(body)
        });
      }
      if (typeof api.getProductDepositRecordData === 'function') {
        await callPaged({
          endpoint: '/api/deposit/depositOperateRecord/searchRecordListData',
          requestUrl: 'ego-service://api.member.getProductDepositRecordData/' + memberId,
          makeBody: (page, size) => ({ page, size, memberId: memberRequestId }),
          fn: (body) => api.getProductDepositRecordData(body)
        });
      }

      if (typeof api.memberSurveys === 'function') {
        await callOnce('GET', '/api/memberSurveys/profile/' + memberId, 'ego-service://api.member.memberSurveys/' + memberId, null, null, () => api.memberSurveys(memberId));
      }
      if (typeof api.memberSurveyRecordData === 'function') {
        const body = { queryData: 'merchantCustomerFormList', merchantId: mch.mch_id, memberId: memberRequestId };
        await callOnce('POST', '/api/tduckDataProxy/query', 'ego-service://api.member.memberSurveyRecordData/' + memberId, {}, body, () => api.memberSurveyRecordData(body));
      }
      if (typeof api.getStorePartnerByMemberId === 'function') {
        await callOnce('GET', '/api/storePartner/getStorePartnerByMemberId/' + memberId, 'ego-service://api.member.getStorePartnerByMemberId/' + memberId, null, null, () => api.getStorePartnerByMemberId(memberId));
      }
    };
"""


def capture_with_ego(
    *,
    task_space: str | int | None,
    entry_url: str,
    start_page: int,
    pages: int,
    page_size: int,
    detail_per_page: int,
    include_asset_overview: bool,
    handoff_on_complete: bool,
    timeout_seconds: int,
) -> dict[str, Any]:
    output_path = Path(tempfile.NamedTemporaryFile(prefix="mei1-ego-", suffix=".json", delete=False).name)
    script = _build_ego_script(
        task_space=task_space,
        entry_url=entry_url,
        start_page=start_page,
        pages=pages,
        page_size=page_size,
        detail_per_page=detail_per_page,
        include_asset_overview=include_asset_overview,
        handoff_on_complete=handoff_on_complete,
        output_path=output_path,
    )
    try:
        completed = subprocess.run(
            ["ego-browser", "nodejs"],
            input=script,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise EgoBrowserError(
                "ego-browser capture failed\n"
                f"stdout:\n{completed.stdout[-4000:]}\n"
                f"stderr:\n{completed.stderr[-4000:]}"
            )
        capture = json.loads(output_path.read_text())
        if not capture.get("ok"):
            raise EgoBrowserError(f"ego-browser page capture failed: {capture}")
        capture["_ego_stdout"] = completed.stdout
        return capture
    finally:
        if output_path.exists():
            output_path.unlink()


def capture_member_details_with_ego(
    *,
    task_space: str | int | None,
    entry_url: str,
    member_ids: list[str],
    include_asset_overview: bool,
    handoff_on_complete: bool,
    timeout_seconds: int,
) -> dict[str, Any]:
    output_path = Path(tempfile.NamedTemporaryFile(prefix="mei1-ego-details-", suffix=".json", delete=False).name)
    script = _build_ego_details_script(
        task_space=task_space,
        entry_url=entry_url,
        member_ids=member_ids,
        include_asset_overview=include_asset_overview,
        handoff_on_complete=handoff_on_complete,
        output_path=output_path,
    )
    try:
        completed = subprocess.run(
            ["ego-browser", "nodejs"],
            input=script,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise EgoBrowserError(
                "ego-browser detail capture failed\n"
                f"stdout:\n{completed.stdout[-4000:]}\n"
                f"stderr:\n{completed.stderr[-4000:]}"
            )
        capture = json.loads(output_path.read_text())
        if not capture.get("ok"):
            raise EgoBrowserError(f"ego-browser detail capture failed: {capture}")
        capture["_ego_stdout"] = completed.stdout
        return capture
    finally:
        if output_path.exists():
            output_path.unlink()


def _build_ego_script(
    *,
    task_space: str | int | None,
    entry_url: str,
    start_page: int,
    pages: int,
    page_size: int,
    detail_per_page: int,
    include_asset_overview: bool,
    handoff_on_complete: bool,
    output_path: Path,
) -> str:
    task_space_json = json.dumps(task_space)
    entry_url_json = json.dumps(entry_url)
    output_path_json = json.dumps(str(output_path))
    handoff_json = json.dumps(handoff_on_complete)
    end_page = start_page + pages - 1
    return f"""
const fs = await import('node:fs')
const taskSpaceRef = {task_space_json}
let taskSpaceId = taskSpaceRef
if (taskSpaceRef === null || taskSpaceRef === undefined || taskSpaceRef === '') {{
  const task = await useOrCreateTaskSpace('mei1 customer crawl')
  taskSpaceId = task.id
}} else {{
  await takeOverTaskSpace(taskSpaceRef)
}}

await gotoAndWait({entry_url_json}, {{ timeout: 30, settle: 3 }})

const result = await js(String.raw`(async () => {{
  const serializeError = (e) => {{
    const copy = {{}};
    for (const k in e || {{}}) copy[k] = e[k];
    return {{ ok: false, name: e?.name, message: e?.message, raw: String(e), copy }};
  }};
  const isPermissionError = (e) => {{
    const text = JSON.stringify(e || '');
    return /无权限|没有.*权限|权限不足|未授权|未登录|unauthorized|forbidden|permission/i.test(text);
  }};
  try {{
    if (!window.angular || !angular.element(document.body).injector()) {{
      return JSON.stringify({{ ok: false, message: 'Angular app is not ready or user is not logged in.', href: location.href, title: document.title }});
    }}
    const injector = angular.element(document.body).injector();
    const api = injector.get('api.member');
    const root = angular.mw.scope;
    const mch = root.mch_info;
    if (!mch || !mch.mch_id || !mch.store_id) {{
      return JSON.stringify({{ ok: false, message: 'Missing merchant/store context.', href: location.href, title: document.title }});
    }}
    const storeIds = '-1,' + mch.store_id;
    const payloads = [];
    const errors = [];
    const seenDetailMemberIds = new Set();
{_DETAIL_CAPTURE_HELPERS_JS}

    for (let pageNo = {start_page}; pageNo <= {end_page}; pageNo++) {{
      const listRequest = {{
        query: [
          {{ field: 'storeIds', value: storeIds }},
          {{ field: 'merchantId', value: mch.mch_id }}
        ],
        sort: [{{ field: 'cardBalance', sort: 'desc' }}],
        page: pageNo,
        size: {page_size}
      }};
      const listResponse = await api.memberList(angular.copy(listRequest));
      payloads.push({{
        method: 'POST',
        endpoint: '/api/member/list/search',
        requestUrl: 'ego-service://api.member.memberList',
        requestParams: {{}},
        requestBody: listRequest,
        statusCode: 200,
        responseJson: listResponse
      }});

      const rows = listResponse?.data?.rows || [];
      const detailRows = rows.slice(0, {detail_per_page});
      for (const row of detailRows) {{
        const memberId = row?.memberId || row?.id;
        if (!memberId || seenDetailMemberIds.has(String(memberId))) continue;
        seenDetailMemberIds.add(String(memberId));

        await captureMemberDetailPayloads(memberId, {str(include_asset_overview).lower()});
      }}
    }}

    return JSON.stringify({{
      ok: true,
      capturedAt: new Date().toISOString(),
      entryUrl: location.href,
      taskSpaceId: null,
      tenant: {{
        merchantId: mch.mch_id,
        merchantName: mch.mch_name,
        storeId: mch.store_id,
        storeName: mch.permission_store_list?.[0]?.name || root.store_name,
        userId: root.user_id,
        userName: root.user_name
      }},
      payloads,
      errors
    }});
  }} catch (e) {{
    return JSON.stringify(serializeError(e));
  }}
}})()`)

fs.writeFileSync({output_path_json}, result, 'utf8')
const parsed = JSON.parse(result)
cliLog(JSON.stringify({{
  ok: parsed.ok,
  taskSpaceId,
  payloads: parsed.payloads ? parsed.payloads.length : 0,
  errors: parsed.errors ? parsed.errors.length : 0,
  output: {output_path_json}
}}, null, 2))
if ({handoff_json}) {{
  await handOffTaskSpace(taskSpaceId)
}}
"""


def _build_ego_details_script(
    *,
    task_space: str | int | None,
    entry_url: str,
    member_ids: list[str],
    include_asset_overview: bool,
    handoff_on_complete: bool,
    output_path: Path,
) -> str:
    task_space_json = json.dumps(task_space)
    entry_url_json = json.dumps(entry_url)
    member_ids_json = json.dumps([str(member_id) for member_id in member_ids])
    output_path_json = json.dumps(str(output_path))
    handoff_json = json.dumps(handoff_on_complete)
    return f"""
const fs = await import('node:fs')
const taskSpaceRef = {task_space_json}
let taskSpaceId = taskSpaceRef
if (taskSpaceRef === null || taskSpaceRef === undefined || taskSpaceRef === '') {{
  const task = await useOrCreateTaskSpace('mei1 customer crawl')
  taskSpaceId = task.id
}} else {{
  await takeOverTaskSpace(taskSpaceRef)
}}

await gotoAndWait({entry_url_json}, {{ timeout: 30, settle: 3 }})

const result = await js(String.raw`(async () => {{
  const memberIds = {member_ids_json};
  const serializeError = (e) => {{
    const copy = {{}};
    for (const k in e || {{}}) copy[k] = e[k];
    return {{ ok: false, name: e?.name, message: e?.message, raw: String(e), copy }};
  }};
  const isPermissionError = (e) => {{
    const text = JSON.stringify(e || '');
    return /无权限|没有.*权限|权限不足|未授权|未登录|unauthorized|forbidden|permission/i.test(text);
  }};
  try {{
    if (!window.angular || !angular.element(document.body).injector()) {{
      return JSON.stringify({{ ok: false, message: 'Angular app is not ready or user is not logged in.', href: location.href, title: document.title }});
    }}
    const injector = angular.element(document.body).injector();
    const api = injector.get('api.member');
    const root = angular.mw.scope;
    const mch = root.mch_info;
    if (!mch || !mch.mch_id || !mch.store_id) {{
      return JSON.stringify({{ ok: false, message: 'Missing merchant/store context.', href: location.href, title: document.title }});
    }}
    const storeIds = '-1,' + mch.store_id;
    const payloads = [];
    const errors = [];
{_DETAIL_CAPTURE_HELPERS_JS}

    for (const memberId of memberIds) {{
      await captureMemberDetailPayloads(memberId, {str(include_asset_overview).lower()});
    }}

    return JSON.stringify({{
      ok: true,
      capturedAt: new Date().toISOString(),
      entryUrl: location.href,
      taskSpaceId: null,
      tenant: {{
        merchantId: mch.mch_id,
        merchantName: mch.mch_name,
        storeId: mch.store_id,
        storeName: mch.permission_store_list?.[0]?.name || root.store_name,
        userId: root.user_id,
        userName: root.user_name
      }},
      payloads,
      errors
    }});
  }} catch (e) {{
    return JSON.stringify(serializeError(e));
  }}
}})()`)

fs.writeFileSync({output_path_json}, result, 'utf8')
const parsed = JSON.parse(result)
cliLog(JSON.stringify({{
  ok: parsed.ok,
  taskSpaceId,
  requestedMembers: {member_ids_json}.length,
  payloads: parsed.payloads ? parsed.payloads.length : 0,
  errors: parsed.errors ? parsed.errors.length : 0,
  output: {output_path_json}
}}, null, 2))
if ({handoff_json}) {{
  await handOffTaskSpace(taskSpaceId)
}}
"""
