#!/usr/bin/env bash
# Merge a finished job's annotations into a labels directory.
#
# Two steps, and the order matters:
#   1. aggregate_labels.py -- expands each accepted trial's {start, end, label}
#      ranges into spans with text and token_count, plus a per-label summary.
#      Only trials the verifier accepted (reward == 1) are exported.
#   2. add_question.py -- copies the original question into every file. Step 1
#      does not write it, so skipping this leaves new files missing a field.
#
# Both steps skip files that already exist, so exporting several jobs into the same
# directory, or re-running after more trials finish, only adds what is new.
#
# Usage (from the setup_bespoke root):
#   ./scripts/export.sh <job-dir> <dataset-dir> <dest-dir>
#
# Example:
#   ./scripts/export.sh jobs/bespoke-stratos-v2 \
#       datasets/bespoke-stratos-traj-analysis output/labels
set -euo pipefail

JOB="${1:?usage: export.sh <job-dir> <dataset-dir> <dest-dir>}"
DATASET="${2:?usage: export.sh <job-dir> <dataset-dir> <dest-dir>}"
DEST="${3:?usage: export.sh <job-dir> <dataset-dir> <dest-dir>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for dir in "$JOB" "$DATASET"; do
    if [ ! -d "$dir" ]; then
        echo "error: not a directory: $dir" >&2
        exit 1
    fi
done

echo "==> aggregating $JOB -> $DEST"
python3 "$HERE/aggregate_labels.py" --job "$JOB" --dataset "$DATASET" --dest "$DEST"

echo
echo "==> adding question"
python3 "$HERE/add_question.py" --labels "$DEST" --dataset "$DATASET"
