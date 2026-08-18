#!/usr/bin/env zsh
set -euo pipefail

cd "${MEI1_PROJECT_DIR:-/Users/leo/project/mei1}"

CONDA_BASE=$(/opt/homebrew/bin/conda info --base)
. "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate mei1-crawler

mei1-crawler crawl-ego-incremental \
  --task-space "${MEI1_EGO_TASK_SPACE:-35}" \
  --start-page "${MEI1_INCREMENTAL_START_PAGE:-1}" \
  --pages "${MEI1_INCREMENTAL_PAGES:-3}" \
  --window-pages "${MEI1_INCREMENTAL_WINDOW_PAGES:-3}" \
  --detail-batch-size "${MEI1_DETAIL_BATCH_SIZE:-10}" \
  --timeout "${MEI1_EGO_TIMEOUT:-240}"
