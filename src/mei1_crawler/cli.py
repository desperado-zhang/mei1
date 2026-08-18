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
from .hashutil import sha256_text


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
    if args.command == "counts":
        return counts(args)
    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mei1-crawler")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path.")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init-db", help="Create or migrate the SQLite schema.")
    sub.add_parser("counts", help="Print key table counts.")

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
    print(f"  skipped_permission_denied: {capture.stats.skipped_permission_denied}")
    if capture.stats.errors:
        print(f"  capture_errors: {len(capture.stats.errors)}")
        for error in capture.stats.errors[:5]:
            print(f"    - {error}")
    print("[mei1-crawler] db counts:")
    for table, count in db.counts().items():
        print(f"  {table}: {count}")


if __name__ == "__main__":
    raise SystemExit(main())
