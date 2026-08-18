#!/usr/bin/env zsh
set -euo pipefail

cd "${MEI1_PROJECT_DIR:-/Users/leo/project/mei1}"

zsh scripts/mei1_full.sh

while true; do
  sleep "${MEI1_INCREMENTAL_INTERVAL_SECONDS:-600}"
  zsh scripts/mei1_incremental.sh
done
