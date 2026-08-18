# Mei1 crawler

Local POC for collecting Mei1 customer list and detail-drawer data into SQLite.

Every run/debug command must activate the Miniconda environment first:

```bash
CONDA_BASE=$(/opt/homebrew/bin/conda info --base)
. "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate mei1-crawler
```

Run the minimum POC:

```bash
mei1-crawler crawl --limit-pages 1 --detail-limit 1
```

Start the local read-only customer viewer:

```bash
mei1-crawler serve --port 8787
```

Then open `http://127.0.0.1:8787/members`. The viewer reads `data/mei1.sqlite`,
supports paginated customer browsing, filters by name/member number/masked phone
/store/grade/date range, opens customer detail pages, and exports the currently
filtered basic customer fields to CSV.

Attach to an already-open Chrome instead of launching a browser:

```bash
mei1-crawler crawl --existing-chrome --cdp-url http://127.0.0.1:9222 --limit-pages 1 --detail-limit 1
```

This mode only works when the existing Chrome was started with a DevTools endpoint such as `--remote-debugging-port=9222`. If that endpoint is unavailable, the command fails instead of opening another browser.

Run from an already-logged-in ego-lite task space:

```bash
mei1-crawler crawl-ego --task-space 35 --pages 3 --detail-per-page 2
```

This command uses the current logged-in Mei1 page state in ego-lite, calls the front-end `api.member` service, and saves the resulting customer list/detail JSON through the same SQLite ingest path as the Playwright capture. By default it only allows up to 10 pages per command; pass `--allow-large` only after a small validation run succeeds.

For longer validation runs, split the crawl into page windows:

```bash
mei1-crawler crawl-ego --task-space 35 --start-page 4 --pages 3 --detail-per-page 2
```

Or let the CLI split the page range automatically:

```bash
mei1-crawler crawl-ego-batch --task-space 35 --start-page 11 --end-page 30 --window-pages 3 --detail-per-page 2
```

Build or refresh the local incremental baseline from already-saved observations:

```bash
mei1-crawler rebuild-sync-state
```

Run one sampled incremental scan. This scans list rows, compares stable local hashes, and fetches detail pages only for new or changed members:

```bash
mei1-crawler crawl-ego-incremental --task-space 35 --start-page 1 --pages 3 --window-pages 3
```

For an operational loop, run the first full crawl once and then scan incrementally every 10 minutes:

```bash
zsh scripts/mei1_full_then_incremental_loop.sh
```

Useful environment overrides:

```bash
MEI1_EGO_TASK_SPACE=35
MEI1_FULL_END_PAGE=126
MEI1_FULL_DETAIL_PER_PAGE=2
MEI1_INCREMENTAL_PAGES=3
MEI1_INCREMENTAL_INTERVAL_SECONDS=600
```

If the package has not been installed in editable mode yet:

```bash
python -m pip install -e .
```

Default behavior:

- Opens a headed Playwright browser with a local profile under `data/browser-profile`.
- If not logged in, flashes the browser tab title and waits for the user to log in.
- If common dialog/popup overlays are present, waits for the user to close them.
- Captures only customer-list and customer-detail related API responses.
- Stores raw API JSON in `source_payloads`.
- Normalizes best-effort list/profile/account-card records into SQLite with deduplication.

Scope guard:

- The default run is a POC: one list page and one customer detail.
- WeChat chat content is intentionally excluded.
- Attachments are metadata-only in the schema; files are not downloaded.
- Permission-denied customer endpoints are skipped and not stored as payloads.
