from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

from .browser import connect_existing_chrome_cdp, goto_and_wait_ready, open_browser, wait_until_ready
from .capture import ApiCapture
from .db import Database
from .ego import EgoBrowserError, capture_member_details_with_ego, capture_with_ego
from .hashutil import sha256_json, sha256_text
from .viewer import serve_viewer


DEFAULT_ENTRY_URL = "https://saas.mei1.com/app/#/member-new/list?index=0&page=1"
DEFAULT_DB_PATH = Path("data/mei1.sqlite")
DEFAULT_PROFILE_DIR = Path("data/browser-profile")
DEFAULT_TAB_URL_CONTAINS = "saas.mei1.com/app/#/member-new/list"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "init-db":
        return init_db(args)
    if args.command == "crawl":
        return crawl(args)
    if args.command == "crawl-ego":
        return crawl_ego(args)
    if args.command == "crawl-ego-batch":
        return crawl_ego_batch(args)
    if args.command == "crawl-ego-incremental":
        return crawl_ego_incremental(args)
    if args.command == "rebuild-sync-state":
        return rebuild_sync_state(args)
    if args.command == "counts":
        return counts(args)
    if args.command == "serve":
        return serve(args)
    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mei1-crawler")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path.")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init-db", help="Create or migrate the SQLite schema.")
    sub.add_parser("counts", help="Print key table counts.")
    sub.add_parser("rebuild-sync-state", help="Backfill list content hashes and rebuild incremental sync baseline.")
    serve_parser = sub.add_parser("serve", help="Start the local read-only customer viewer.")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Host interface to bind.")
    serve_parser.add_argument("--port", type=int, default=8787, help="HTTP port to bind.")

    crawl_parser = sub.add_parser("crawl", help="Run the POC crawl.")
    crawl_parser.add_argument("--entry-url", default=DEFAULT_ENTRY_URL)
    crawl_parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    crawl_parser.add_argument("--limit-pages", type=int, default=1)
    crawl_parser.add_argument("--detail-limit", type=int, default=1)
    crawl_parser.add_argument(
        "--login-timeout",
        type=int,
        default=0,
        help="Seconds to wait for manual login and popup closing. 0 means wait forever.",
    )
    crawl_parser.add_argument("--headless", action="store_true", help="Run headless. Not recommended for manual login.")
    crawl_parser.add_argument(
        "--existing-chrome",
        action="store_true",
        help="Attach to an already-running Chrome CDP endpoint. Never launches a browser or creates a tab.",
    )
    crawl_parser.add_argument(
        "--cdp-url",
        default=os.environ.get("MEI1_CDP_URL", "http://127.0.0.1:9222"),
        help="Chrome DevTools endpoint used with --existing-chrome.",
    )
    crawl_parser.add_argument(
        "--tab-url-contains",
        default=DEFAULT_TAB_URL_CONTAINS,
        help="Existing tab URL substring required with --existing-chrome.",
    )
    crawl_parser.add_argument("--list-timeout", type=int, default=30, help="Seconds to wait for the list API.")
    crawl_parser.add_argument("--detail-timeout", type=int, default=30, help="Seconds to wait for detail APIs.")

    ego_parser = sub.add_parser("crawl-ego", help="Run the ego-lite crawler from a logged-in task space.")
    ego_parser.add_argument("--entry-url", default=DEFAULT_ENTRY_URL)
    ego_parser.add_argument(
        "--task-space",
        default=None,
        help="Existing ego-lite task space id/name. Use this after manual login, e.g. 35.",
    )
    ego_parser.add_argument("--start-page", type=int, default=1, help="First member-list page to crawl.")
    ego_parser.add_argument("--pages", type=int, default=3, help="List pages to crawl.")
    ego_parser.add_argument("--page-size", type=int, default=20, help="Rows per member-list page.")
    ego_parser.add_argument("--detail-per-page", type=int, default=2, help="Detail records to fetch per list page.")
    ego_parser.add_argument("--timeout", type=int, default=180, help="Seconds before aborting the ego-lite command.")
    ego_parser.add_argument(
        "--allow-large",
        action="store_true",
        help="Allow more than 10 pages in one command.",
    )
    ego_parser.add_argument(
        "--skip-asset-overview",
        action="store_true",
        help="Do not probe /api/member/amount/{id}. Permission-denied responses are skipped either way.",
    )

    ego_batch_parser = sub.add_parser("crawl-ego-batch", help="Run ego-lite crawler across multiple page windows.")
    ego_batch_parser.add_argument("--entry-url", default=DEFAULT_ENTRY_URL)
    ego_batch_parser.add_argument(
        "--task-space",
        default=None,
        help="Existing ego-lite task space id/name. Use this after manual login, e.g. 35.",
    )
    ego_batch_parser.add_argument("--start-page", type=int, required=True, help="First member-list page to crawl.")
    ego_batch_parser.add_argument("--end-page", type=int, required=True, help="Last member-list page to crawl.")
    ego_batch_parser.add_argument(
        "--window-pages",
        type=int,
        default=3,
        help="Pages per ego-lite execution window. Smaller windows avoid Runtime.evaluate timeouts.",
    )
    ego_batch_parser.add_argument("--page-size", type=int, default=20, help="Rows per member-list page.")
    ego_batch_parser.add_argument("--detail-per-page", type=int, default=2, help="Detail records to fetch per list page.")
    ego_batch_parser.add_argument("--timeout", type=int, default=240, help="Seconds before aborting one ego-lite window.")
    ego_batch_parser.add_argument(
        "--allow-large",
        action="store_true",
        help="Allow more than 10 pages in a single execution window.",
    )
    ego_batch_parser.add_argument(
        "--skip-asset-overview",
        action="store_true",
        help="Do not probe /api/member/amount/{id}. Permission-denied responses are skipped either way.",
    )

    incremental_parser = sub.add_parser(
        "crawl-ego-incremental",
        help="Run a sampled list scan and fetch details only for new or changed members.",
    )
    incremental_parser.add_argument("--entry-url", default=DEFAULT_ENTRY_URL)
    incremental_parser.add_argument(
        "--task-space",
        default=None,
        help="Existing ego-lite task space id/name. Use this after manual login, e.g. 35.",
    )
    incremental_parser.add_argument("--start-page", type=int, default=1, help="First sampled member-list page.")
    incremental_parser.add_argument("--pages", type=int, default=3, help="Sampled list pages to scan.")
    incremental_parser.add_argument(
        "--window-pages",
        type=int,
        default=3,
        help="List pages per ego-lite execution window.",
    )
    incremental_parser.add_argument("--page-size", type=int, default=20, help="Rows per member-list page.")
    incremental_parser.add_argument(
        "--detail-batch-size",
        type=int,
        default=10,
        help="Changed/new member IDs per detail fetch window.",
    )
    incremental_parser.add_argument("--timeout", type=int, default=240, help="Seconds before aborting one ego-lite window.")
    incremental_parser.add_argument(
        "--allow-large",
        action="store_true",
        help="Allow more than 10 pages in a single list window.",
    )
    incremental_parser.add_argument(
        "--skip-asset-overview",
        action="store_true",
        help="Do not probe /api/member/amount/{id}. Permission-denied responses are skipped either way.",
    )

    return parser


def init_db(args: argparse.Namespace) -> int:
    db = Database(args.db)
    try:
        db.init_schema()
        print(f"initialized: {args.db}")
        return 0
    finally:
        db.close()


def counts(args: argparse.Namespace) -> int:
    db = Database(args.db)
    try:
        db.init_schema()
        for table, count in db.counts().items():
            print(f"{table}: {count}")
        return 0
    finally:
        db.close()


def serve(args: argparse.Namespace) -> int:
    try:
        serve_viewer(args.db, host=args.host, port=args.port)
    except OSError as exc:
        print(f"[mei1-viewer] failed: {exc}", file=sys.stderr)
        return 1
    return 0


def crawl(args: argparse.Namespace) -> int:
    if args.limit_pages != 1:
        print("POC guard: only --limit-pages 1 is implemented in this step.", file=sys.stderr)
        return 2
    if args.detail_limit < 0:
        print("--detail-limit must be >= 0", file=sys.stderr)
        return 2
    if args.headless and args.login_timeout == 0:
        print("Headless mode cannot support manual login with an infinite wait.", file=sys.stderr)
        return 2
    if args.existing_chrome and args.headless:
        print("--headless is not valid with --existing-chrome.", file=sys.stderr)
        return 2

    db = Database(args.db)
    run_id: int | None = None
    try:
        db.init_schema()
        mode = "chrome-cdp" if args.existing_chrome else "playwright"
        run_id = db.start_run(tenant_key=None, entry_url=args.entry_url, mode=mode)
        print(f"[mei1-crawler] run_id={run_id} db={args.db}")

        if args.existing_chrome:
            print(f"[mei1-crawler] attaching existing Chrome via CDP: {args.cdp_url}")
            with connect_existing_chrome_cdp(args.cdp_url, tab_url_contains=args.tab_url_contains) as (_context, page):
                print("[mei1-crawler] using existing customer-list tab")
                wait_until_ready(page, timeout_seconds=args.login_timeout)
                return crawl_ready_page(args, db, run_id, page)

        with open_browser(args.profile_dir, headed=not args.headless) as (_context, page):
            print("[mei1-crawler] opening list page")
            goto_and_wait_ready(page, args.entry_url, timeout_seconds=args.login_timeout)
            return crawl_ready_page(args, db, run_id, page)
    except KeyboardInterrupt:
        if run_id:
            db.finish_run(run_id, "cancelled", "Interrupted by user.")
        raise
    except Exception as exc:  # noqa: BLE001 - CLI should close run cleanly.
        if run_id:
            db.finish_run(run_id, "failed", f"{type(exc).__name__}: {exc}")
        print(f"[mei1-crawler] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


def crawl_ego(args: argparse.Namespace) -> int:
    validation_error = validate_ego_args(args)
    if validation_error:
        print(validation_error, file=sys.stderr)
        return 2

    return run_ego_window(args, start_page=args.start_page, pages=args.pages, handoff_on_complete=True)


def crawl_ego_batch(args: argparse.Namespace) -> int:
    validation_error = validate_ego_batch_args(args)
    if validation_error:
        print(validation_error, file=sys.stderr)
        return 2

    total_pages = args.end_page - args.start_page + 1
    print(
        "[mei1-crawler] ego-lite batch "
        f"task_space={args.task_space or 'auto'} start_page={args.start_page} "
        f"end_page={args.end_page} total_pages={total_pages} window_pages={args.window_pages}"
    )
    current_page = args.start_page
    window_index = 1
    while current_page <= args.end_page:
        pages = min(args.window_pages, args.end_page - current_page + 1)
        last_page = current_page + pages - 1
        handoff_on_complete = last_page >= args.end_page
        print(f"[mei1-crawler] batch window {window_index}: pages {current_page}-{last_page}")
        code = run_ego_window(
            args,
            start_page=current_page,
            pages=pages,
            handoff_on_complete=handoff_on_complete,
        )
        if code != 0:
            print(f"[mei1-crawler] batch stopped at pages {current_page}-{last_page}", file=sys.stderr)
            return code
        current_page = last_page + 1
        window_index += 1
    print("[mei1-crawler] ego-lite batch completed")
    return 0


def crawl_ego_incremental(args: argparse.Namespace) -> int:
    validation_error = validate_ego_incremental_args(args)
    if validation_error:
        print(validation_error, file=sys.stderr)
        return 2

    ensure_sync_baseline(args)
    print(
        "[mei1-crawler] ego-lite incremental scan "
        f"task_space={args.task_space or 'auto'} start_page={args.start_page} "
        f"pages={args.pages} window_pages={args.window_pages}"
    )
    detail_targets: list[str] = []
    detail_reasons: dict[str, str] = {}
    current_page = args.start_page
    end_page = args.start_page + args.pages - 1
    window_index = 1
    while current_page <= end_page:
        pages = min(args.window_pages, end_page - current_page + 1)
        print(f"[mei1-crawler] incremental list window {window_index}: pages {current_page}-{current_page + pages - 1}")
        code, capture = run_ego_list_scan_window(args, start_page=current_page, pages=pages)
        if code != 0:
            return code
        for source_id in capture.detail_target_source_ids:
            if source_id not in detail_reasons:
                detail_targets.append(source_id)
                detail_reasons[source_id] = capture.detail_target_reasons.get(source_id, "changed")
        current_page += pages
        window_index += 1

    print(
        "[mei1-crawler] incremental scan result "
        f"detail_targets={len(detail_targets)}"
    )
    if not detail_targets:
        capture_member_details_with_ego(
            task_space=args.task_space,
            entry_url=args.entry_url,
            member_ids=[],
            include_asset_overview=not args.skip_asset_overview,
            handoff_on_complete=True,
            timeout_seconds=args.timeout,
        )
        return 0

    detail_batches = [
        detail_targets[index : index + args.detail_batch_size]
        for index in range(0, len(detail_targets), args.detail_batch_size)
    ]
    for index, batch in enumerate(detail_batches, start=1):
        print(f"[mei1-crawler] incremental detail window {index}: members={len(batch)}")
        code = run_ego_detail_window(
            args,
            member_ids=batch,
            detail_reasons=detail_reasons,
            handoff_on_complete=index == len(detail_batches),
        )
        if code != 0:
            return code
    return 0


def rebuild_sync_state(args: argparse.Namespace) -> int:
    db = Database(args.db)
    try:
        db.init_schema()
        backfilled = db.backfill_list_content_hashes(force=True)
        rebuilt = db.rebuild_sync_state_from_observations()
        print(f"[mei1-crawler] list row_content_hash backfilled: {backfilled}")
        print(f"[mei1-crawler] sync states rebuilt: {rebuilt}")
        for table, count in db.sync_state_counts().items():
            print(f"{table}: {count}")
        return 0
    finally:
        db.close()


def ensure_sync_baseline(args: argparse.Namespace) -> None:
    db = Database(args.db)
    try:
        db.init_schema()
        counts = db.sync_state_counts()
        observation_count = db.conn.execute(
            "SELECT count(*) FROM member_list_observations"
        ).fetchone()[0]
        if counts["member_sync_states"] == 0 and observation_count:
            print("[mei1-crawler] sync baseline is empty; rebuilding from existing observations")
            db.backfill_list_content_hashes()
            db.rebuild_sync_state_from_observations()
    finally:
        db.close()


def run_ego_list_scan_window(args: argparse.Namespace, *, start_page: int, pages: int) -> tuple[int, ApiCapture]:
    db = Database(args.db)
    run_id: int | None = None
    try:
        db.init_schema()
        run_id = db.start_run(tenant_key=None, entry_url=args.entry_url, mode="ego-lite-incremental-list")
        print(f"[mei1-crawler] run_id={run_id} db={args.db}")
        capture_result = capture_with_ego(
            task_space=args.task_space,
            entry_url=args.entry_url,
            start_page=start_page,
            pages=pages,
            page_size=args.page_size,
            detail_per_page=0,
            include_asset_overview=not args.skip_asset_overview,
            handoff_on_complete=False,
            timeout_seconds=args.timeout,
        )
        capture = ingest_ego_capture_result(args, db, run_id, capture_result)
        code = finish_ego_run(db, run_id, capture)
        return code, capture
    except KeyboardInterrupt:
        if run_id:
            db.finish_run(run_id, "cancelled", "Interrupted by user.")
        raise
    except EgoBrowserError as exc:
        if run_id:
            db.finish_run(run_id, "failed", str(exc)[:1000])
        print(f"[mei1-crawler] ego-lite failed: {exc}", file=sys.stderr)
        return 1, ApiCapture(db, run_id=run_id or 0, tenant_key="unknown")
    except Exception as exc:  # noqa: BLE001 - CLI should close run cleanly.
        if run_id:
            db.finish_run(run_id, "failed", f"{type(exc).__name__}: {exc}")
        print(f"[mei1-crawler] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1, ApiCapture(db, run_id=run_id or 0, tenant_key="unknown")
    finally:
        db.close()


def run_ego_detail_window(
    args: argparse.Namespace,
    *,
    member_ids: list[str],
    detail_reasons: dict[str, str],
    handoff_on_complete: bool,
) -> int:
    db = Database(args.db)
    run_id: int | None = None
    try:
        db.init_schema()
        run_id = db.start_run(tenant_key=None, entry_url=args.entry_url, mode="ego-lite-incremental-detail")
        print(f"[mei1-crawler] run_id={run_id} db={args.db}")
        capture_result = capture_member_details_with_ego(
            task_space=args.task_space,
            entry_url=args.entry_url,
            member_ids=member_ids,
            include_asset_overview=not args.skip_asset_overview,
            handoff_on_complete=handoff_on_complete,
            timeout_seconds=args.timeout,
        )
        capture = ingest_ego_capture_result(args, db, run_id, capture_result)
        for source_id in member_ids:
            member_id = db.find_member_id(tenant_key=capture.tenant_key, source_member_id=source_id)
            if member_id is not None:
                db.mark_detail_requested(member_id, detail_reasons.get(source_id, "changed"))
        return finish_ego_run(db, run_id, capture, allow_detail_only=True)
    except KeyboardInterrupt:
        if run_id:
            db.finish_run(run_id, "cancelled", "Interrupted by user.")
        raise
    except EgoBrowserError as exc:
        if run_id:
            db.finish_run(run_id, "failed", str(exc)[:1000])
        print(f"[mei1-crawler] ego-lite failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI should close run cleanly.
        if run_id:
            db.finish_run(run_id, "failed", f"{type(exc).__name__}: {exc}")
        print(f"[mei1-crawler] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


def run_ego_window(
    args: argparse.Namespace,
    *,
    start_page: int,
    pages: int,
    handoff_on_complete: bool,
) -> int:
    db = Database(args.db)
    run_id: int | None = None
    try:
        db.init_schema()
        run_id = db.start_run(tenant_key=None, entry_url=args.entry_url, mode="ego-lite")
        print(f"[mei1-crawler] run_id={run_id} db={args.db}")
        print(
            "[mei1-crawler] ego-lite capture "
            f"task_space={args.task_space or 'auto'} pages={pages} "
            f"start_page={start_page} page_size={args.page_size} detail_per_page={args.detail_per_page}"
        )
        capture_result = capture_with_ego(
            task_space=args.task_space,
            entry_url=args.entry_url,
            start_page=start_page,
            pages=pages,
            page_size=args.page_size,
            detail_per_page=args.detail_per_page,
            include_asset_overview=not args.skip_asset_overview,
            handoff_on_complete=handoff_on_complete,
            timeout_seconds=args.timeout,
        )

        capture = ingest_ego_capture_result(args, db, run_id, capture_result)
        return finish_ego_run(db, run_id, capture)
    except KeyboardInterrupt:
        if run_id:
            db.finish_run(run_id, "cancelled", "Interrupted by user.")
        raise
    except EgoBrowserError as exc:
        if run_id:
            db.finish_run(run_id, "failed", str(exc)[:1000])
        print(f"[mei1-crawler] ego-lite failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI should close run cleanly.
        if run_id:
            db.finish_run(run_id, "failed", f"{type(exc).__name__}: {exc}")
        print(f"[mei1-crawler] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


def ingest_ego_capture_result(args: argparse.Namespace, db: Database, run_id: int, capture_result: dict[str, Any]) -> ApiCapture:
    tenant = capture_result.get("tenant") if isinstance(capture_result.get("tenant"), dict) else {}
    tenant_key = tenant_key_from_ego(tenant)
    db.upsert_tenant(
        tenant_key,
        merchant_id=_optional_str(tenant.get("merchantId")),
        merchant_name=_optional_str(tenant.get("merchantName")),
        store_id=_optional_str(tenant.get("storeId")),
        store_name=_optional_str(tenant.get("storeName")),
        operator_name=_optional_str(tenant.get("userName")),
        source_account_label=_optional_str(tenant.get("userId")),
        raw_json=tenant,
    )
    db.update_run(run_id, tenant_key=tenant_key)

    capture = ApiCapture(db, run_id=run_id, tenant_key=tenant_key)
    for payload in capture_result.get("payloads", []):
        if not isinstance(payload, dict):
            continue
        capture.process_payload(
            method=_optional_str(payload.get("method")) or "GET",
            endpoint=_optional_str(payload.get("endpoint")) or "",
            request_url=_optional_str(payload.get("requestUrl")) or "",
            request_params=payload.get("requestParams"),
            request_body=payload.get("requestBody"),
            status_code=_optional_int(payload.get("statusCode")),
            response_json=payload.get("responseJson"),
        )

    for error in capture_result.get("errors", []):
        if not isinstance(error, dict):
            continue
        endpoint = _optional_str(error.get("endpoint")) or "unknown"
        if error.get("permissionDenied") or _looks_permission_error(error):
            capture.stats.skipped_permission_denied += 1
            db.add_event(run_id, "permission_denied", f"Skipped permission-denied endpoint: {endpoint}", error)
        else:
            message = f"ego capture error on {endpoint}: {error.get('error')}"
            capture.stats.errors.append(message)
            db.add_event(run_id, "capture_error", message, error)
    return capture


def finish_ego_run(
    db: Database,
    run_id: int,
    capture: ApiCapture,
    *,
    allow_detail_only: bool = False,
) -> int:
    db.update_run(
        run_id,
        page_count=capture.stats.list_pages,
        member_count=capture.stats.list_rows,
    )
    succeeded = capture.stats.list_pages > 0 or (allow_detail_only and capture.stats.detail_profiles > 0)
    status = "success" if succeeded else "failed"
    notes = None if succeeded else "No member list or detail payload was captured."
    if capture.stats.errors:
        notes = (notes or "") + f" capture_errors={len(capture.stats.errors)}"
    if capture.stats.skipped_permission_denied:
        notes = (notes or "") + f" skipped_permission_denied={capture.stats.skipped_permission_denied}"
    db.finish_run(run_id, status, notes)
    print_summary(db, capture)
    return 0 if status == "success" else 1


def validate_ego_args(args: argparse.Namespace) -> str | None:
    if args.start_page < 1:
        return "--start-page must be >= 1"
    if args.pages < 1:
        return "--pages must be >= 1"
    if args.pages > 10 and not args.allow_large:
        return "Safety guard: --pages > 10 requires --allow-large."
    if args.page_size < 1 or args.page_size > 100:
        return "--page-size must be between 1 and 100"
    if args.detail_per_page < 0:
        return "--detail-per-page must be >= 0"
    if args.timeout <= 0:
        return "--timeout must be > 0"
    return None


def validate_ego_batch_args(args: argparse.Namespace) -> str | None:
    if args.start_page < 1:
        return "--start-page must be >= 1"
    if args.end_page < args.start_page:
        return "--end-page must be >= --start-page"
    if args.window_pages < 1:
        return "--window-pages must be >= 1"
    if args.window_pages > 10 and not args.allow_large:
        return "Safety guard: --window-pages > 10 requires --allow-large."
    if args.page_size < 1 or args.page_size > 100:
        return "--page-size must be between 1 and 100"
    if args.detail_per_page < 0:
        return "--detail-per-page must be >= 0"
    if args.timeout <= 0:
        return "--timeout must be > 0"
    return None


def validate_ego_incremental_args(args: argparse.Namespace) -> str | None:
    if args.start_page < 1:
        return "--start-page must be >= 1"
    if args.pages < 1:
        return "--pages must be >= 1"
    if args.window_pages < 1:
        return "--window-pages must be >= 1"
    if args.window_pages > 10 and not args.allow_large:
        return "Safety guard: --window-pages > 10 requires --allow-large."
    if args.page_size < 1 or args.page_size > 100:
        return "--page-size must be between 1 and 100"
    if args.detail_batch_size < 1:
        return "--detail-batch-size must be >= 1"
    if args.timeout <= 0:
        return "--timeout must be > 0"
    return None


def tenant_key_from_ego(tenant: dict[str, Any]) -> str:
    merchant_id = _optional_str(tenant.get("merchantId"))
    store_id = _optional_str(tenant.get("storeId"))
    if merchant_id and store_id:
        return f"merchant:{merchant_id}:store:{store_id}"
    return "mei1:ego:" + sha256_json(tenant)[:16]


def crawl_ready_page(args: argparse.Namespace, db: Database, run_id: int, page: Any) -> int:
    tenant_key = derive_tenant_key(page)
    db.upsert_tenant(tenant_key, raw_json={"derived_from_page": True})
    db.update_run(run_id, tenant_key=tenant_key)

    capture = ApiCapture(db, run_id=run_id, tenant_key=tenant_key)
    capture.attach(page)

    print("[mei1-crawler] page ready; reloading once to capture member list API")
    page.reload(wait_until="domcontentloaded")
    wait_until_ready(page, timeout_seconds=args.login_timeout)
    print("[mei1-crawler] waiting for member list API")
    if not capture.wait_for(lambda stats: stats.list_pages >= 1, args.list_timeout):
        print("[mei1-crawler] list API not captured yet; reloading once")
        page.reload(wait_until="domcontentloaded")
        wait_until_ready(page, timeout_seconds=args.login_timeout)
        capture.wait_for(lambda stats: stats.list_pages >= 1, args.list_timeout)

    if args.detail_limit > 0:
        open_first_detail(page)
        capture.wait_for(lambda stats: stats.detail_profiles >= args.detail_limit, args.detail_timeout)
        close_detail_drawer(page)

    db.update_run(
        run_id,
        page_count=capture.stats.list_pages,
        member_count=capture.stats.list_rows,
    )
    status = "success" if capture.stats.list_pages else "failed"
    notes = None if capture.stats.list_pages else "No member list API payload was captured."
    if capture.stats.errors:
        notes = (notes or "") + f" capture_errors={len(capture.stats.errors)}"
    if capture.stats.skipped_permission_denied:
        notes = (notes or "") + f" skipped_permission_denied={capture.stats.skipped_permission_denied}"
    db.finish_run(run_id, status, notes)
    print_summary(db, capture)
    return 0 if status == "success" else 1


def derive_tenant_key(page: Any) -> str:
    try:
        text = page.locator("body").inner_text(timeout=3_000)
    except Exception:
        return "mei1:default"

    store_match = re.search(r"([\u4e00-\u9fffA-Za-z0-9·()（）—\-]+店[^/\n]{0,80})\s*/\s*([^\n]{1,40})", text)
    if store_match:
        raw = " | ".join(part.strip() for part in store_match.groups())
    else:
        raw = text[:400]
    return "mei1:" + sha256_text(raw)[:16]


def open_first_detail(page: Any) -> None:
    print("[mei1-crawler] opening first customer detail")
    locator = page.get_by_text("详情", exact=True).first
    try:
        locator.click(timeout=8_000)
        return
    except Exception:
        pass

    clicked = page.evaluate(
        """
        () => {
          const visible = (el) => {
            const style = getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
          };
          const nodes = Array.from(document.querySelectorAll('a,button,[role="button"],span,div'));
          const target = nodes.find(el => visible(el) && (el.innerText || el.textContent || '').trim() === '详情');
          if (!target) return false;
          target.click();
          return true;
        }
        """
    )
    if not clicked:
        raise RuntimeError("Could not find a visible detail entry on the list page.")


def close_detail_drawer(page: Any) -> None:
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
    except Exception:
        pass
    try:
        page.evaluate(
            """
            () => {
              const candidates = Array.from(document.querySelectorAll('button, [role="button"], md-icon, i'));
              const target = candidates.find((el) => {
                const text = (el.innerText || el.textContent || '').trim();
                const aria = el.getAttribute('aria-label') || '';
                return text === 'close' || text === '×' || aria.includes('关闭') || aria.toLowerCase().includes('close');
              });
              if (target) target.click();
            }
            """
        )
    except Exception:
        pass


def print_summary(db: Database, capture: ApiCapture) -> None:
    print("[mei1-crawler] capture stats:")
    print(f"  source_payloads: {capture.stats.source_payloads}")
    print(f"  list_pages: {capture.stats.list_pages}")
    print(f"  list_rows: {capture.stats.list_rows}")
    print(f"  detail_profiles: {capture.stats.detail_profiles}")
    print(f"  asset_snapshots: {capture.stats.asset_snapshots}")
    print(f"  account_items: {capture.stats.account_items}")
    print(f"  service_records: {capture.stats.service_records}")
    print(f"  detail_records: {capture.stats.detail_records}")
    print(f"  survey_profiles: {capture.stats.survey_profiles}")
    print(f"  attachments: {capture.stats.attachments}")
    print(f"  partner_infos: {capture.stats.partner_infos}")
    print(f"  new_members: {capture.stats.new_members}")
    print(f"  changed_members: {capture.stats.changed_members}")
    print(f"  unchanged_members: {capture.stats.unchanged_members}")
    print(f"  detail_targets: {len(capture.detail_target_source_ids)}")
    print(f"  skipped_permission_denied: {capture.stats.skipped_permission_denied}")
    if capture.stats.errors:
        print(f"  capture_errors: {len(capture.stats.errors)}")
        for error in capture.stats.errors[:5]:
            print(f"    - {error}")
    print("[mei1-crawler] db counts:")
    for table, count in db.counts().items():
        print(f"  {table}: {count}")


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _looks_permission_error(value: Any) -> bool:
    return bool(
        re.search(
            r"无权限|没有.*权限|权限不足|未授权|未登录|unauthorized|forbidden|permission denied",
            str(value),
            re.I,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
