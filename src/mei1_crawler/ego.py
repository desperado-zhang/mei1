from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class EgoBrowserError(RuntimeError):
    pass


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
    let memberOverviewPermissionDenied = false;

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

        const calls = [
          ['GET', '/api/member/detailInfo/' + memberId, 'ego-service://api.member.memberInfo/' + memberId, null, () => api.memberInfo(memberId)],
          ['GET', '/api/member/detail/' + memberId, 'ego-service://api.member.memberDetailOfBooking/' + memberId, null, () => api.memberDetailOfBooking(memberId)]
        ];
        if ({str(include_asset_overview).lower()} && !memberOverviewPermissionDenied) {{
          calls.push(['GET', '/api/member/amount/' + memberId, 'ego-service://api.member.memberOverview/' + memberId, {{ storeIds }}, () => api.memberOverview(memberId, storeIds)]);
        }}

        for (const [method, endpoint, requestUrl, requestParams, fn] of calls) {{
          try {{
            const responseJson = await fn();
            payloads.push({{
              method,
              endpoint,
              requestUrl,
              requestParams: requestParams || {{}},
              requestBody: null,
              statusCode: 200,
              responseJson
            }});
          }} catch (e) {{
            const error = serializeError(e);
            errors.push({{ endpoint, permissionDenied: isPermissionError(error), error }});
            if (endpoint.includes('/api/member/amount/') && isPermissionError(error)) {{
              memberOverviewPermissionDenied = true;
            }}
          }}
        }}
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
    let memberOverviewPermissionDenied = false;

    for (const memberId of memberIds) {{
      const calls = [
        ['GET', '/api/member/detailInfo/' + memberId, 'ego-service://api.member.memberInfo/' + memberId, null, () => api.memberInfo(memberId)],
        ['GET', '/api/member/detail/' + memberId, 'ego-service://api.member.memberDetailOfBooking/' + memberId, null, () => api.memberDetailOfBooking(memberId)]
      ];
      if ({str(include_asset_overview).lower()} && !memberOverviewPermissionDenied) {{
        calls.push(['GET', '/api/member/amount/' + memberId, 'ego-service://api.member.memberOverview/' + memberId, {{ storeIds }}, () => api.memberOverview(memberId, storeIds)]);
      }}

      for (const [method, endpoint, requestUrl, requestParams, fn] of calls) {{
        try {{
          const responseJson = await fn();
          payloads.push({{
            method,
            endpoint,
            requestUrl,
            requestParams: requestParams || {{}},
            requestBody: null,
            statusCode: 200,
            responseJson
          }});
        }} catch (e) {{
          const error = serializeError(e);
          errors.push({{ endpoint, permissionDenied: isPermissionError(error), error }});
          if (endpoint.includes('/api/member/amount/') && isPermissionError(error)) {{
            memberOverviewPermissionDenied = true;
          }}
        }}
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
  requestedMembers: {member_ids_json}.length,
  payloads: parsed.payloads ? parsed.payloads.length : 0,
  errors: parsed.errors ? parsed.errors.length : 0,
  output: {output_path_json}
}}, null, 2))
if ({handoff_json}) {{
  await handOffTaskSpace(taskSpaceId)
}}
"""
