#!/usr/bin/env zsh
set -euo pipefail

cd "${MEI1_PROJECT_DIR:-/Users/leo/project/mei1}"

CONDA_BASE=$(/opt/homebrew/bin/conda info --base)
. "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate mei1-crawler

mei1-crawler crawl-ego-batch \
  --task-space "${MEI1_EGO_TASK_SPACE:-35}" \
  --start-page "${MEI1_FULL_START_PAGE:-1}" \
  --end-page "${MEI1_FULL_END_PAGE:-126}" \
  --window-pages "${MEI1_FULL_WINDOW_PAGES:-3}" \
  --detail-per-page "${MEI1_FULL_DETAIL_PER_PAGE:-2}" \
  --timeout "${MEI1_EGO_TIMEOUT:-240}"

mei1-crawler rebuild-sync-state
