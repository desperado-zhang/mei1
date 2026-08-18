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

Attach to an already-open Chrome instead of launching a browser:

```bash
mei1-crawler crawl --existing-chrome --cdp-url http://127.0.0.1:9222 --limit-pages 1 --detail-limit 1
```

This mode only works when the existing Chrome was started with a DevTools endpoint such as `--remote-debugging-port=9222`. If that endpoint is unavailable, the command fails instead of opening another browser.

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
- Normalizes best-effort list/profile records into SQLite with deduplication.

Scope guard:

- The default run is a POC: one list page and one customer detail.
- WeChat chat content is intentionally excluded.
- Attachments are metadata-only in the schema; files are not downloaded.
- Permission-denied customer endpoints are skipped and not stored as payloads.
