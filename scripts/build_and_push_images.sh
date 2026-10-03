#!/usr/bin/env bash
# Build and push one image per generated task.
#
# Harbor's Kubernetes environment (`ack`) can only build images in-cluster with a
# privileged Docker-in-Docker Job or an external BuildKit daemon. NRP does not
# allow privileged pods, so images are built here on the host and pushed to a
# registry; the job config then runs with force_build: false.
#
# Each task.toml must already pin [environment] docker_image, i.e. the tasks were
# generated with `--registry <registry> [--image-tag <tag>]`.
#
# Pushes are network-latency bound (~12s each serially, ~4s each at -P 8 and
# above, measured against gitlab-registry.nrp-nautilus.io), so they run in
# parallel.
#
# A successful push drops a `.pushed` marker in the task dir, and marked tasks are
# skipped, so re-running after an interruption is instant. Registry-side checks
# are deliberately not used for this: `docker manifest inspect` re-authenticates
# every call (~23s each against NRP GitLab), which for 6000 tasks would cost more
# than the pushes themselves. Set VERIFY=1 to pay that cost and check the registry
# instead of trusting the markers.
#
# Usage:
#   ./scripts/build_and_push_images.sh <dataset-dir> [parallelism] [platform]
#
# Examples:
#   ./scripts/build_and_push_images.sh datasets/bespoke-stratos-traj-analysis
#   ./scripts/build_and_push_images.sh datasets/bespoke-stratos-traj-analysis 16
#   FORCE=1 ./scripts/build_and_push_images.sh datasets/... 16   # rebuild all
#   VERIFY=1 ./scripts/build_and_push_images.sh datasets/... 16  # trust registry, not markers
set -euo pipefail

DATASET_DIR="${1:?usage: build_and_push_images.sh <dataset-dir> [parallelism] [platform]}"
PARALLELISM="${2:-8}"
PLATFORM="${3:-linux/amd64}"
FORCE="${FORCE:-0}"
VERIFY="${VERIFY:-0}"
# Progress log. Truncated on every run -- do not read a stale one when
# checking progress; the path is printed again in the summary.
LOG_FILE="${LOG_FILE:-/tmp/hb_push_log.txt}"

if [ ! -d "$DATASET_DIR" ]; then
    echo "error: dataset dir not found: $DATASET_DIR" >&2
    exit 1
fi

image_of() {
    sed -n 's/^docker_image[[:space:]]*=[[:space:]]*"\(.*\)"$/\1/p' "$1/task.toml" | head -1
}

# Worker for a single task dir. Exported so the xargs subshells can call it.
build_one() {
    task_dir="$1"
    image=$(sed -n 's/^docker_image[[:space:]]*=[[:space:]]*"\(.*\)"$/\1/p' "$task_dir/task.toml" | head -1)
    if [ -z "$image" ]; then
        echo "skip   $(basename "$task_dir") -- no docker_image (regenerate with --registry)" >&2
        return 0
    fi
    if [ "$FORCE" != "1" ]; then
        if [ "$VERIFY" = "1" ]; then
            if docker manifest inspect "$image" >/dev/null 2>&1; then
                # Record the confirmation so later runs can skip without paying
                # for another registry round-trip.
                touch "$task_dir/.pushed"
                echo "exists $(basename "$task_dir")"
                return 0
            fi
        elif [ -f "$task_dir/.pushed" ]; then
            echo "exists $(basename "$task_dir")"
            return 0
        fi
    fi
    if docker buildx build --platform "$PLATFORM" --tag "$image" --push \
        "$task_dir/environment" >/dev/null 2>&1; then
        touch "$task_dir/.pushed"
        echo "pushed $(basename "$task_dir")"
    else
        # stdout, not stderr, so failures land in LOG_FILE next to the
        # successes and can be counted the same way.
        echo "FAILED $(basename "$task_dir")"
        return 1
    fi
}
export -f build_one
export PLATFORM FORCE VERIFY

# Materialize the task list once. Reading it from a file rather than piping
# `find | sort` into `head` matters: under `set -o pipefail`, `head` closing the
# pipe early makes `sort` die of SIGPIPE (exit 141), which `set -e` then turns
# into a silent exit of this whole script. That only shows up once the list is
# big enough for `head` to win the race -- fine at 20 tasks, fatal at 6000.
TASK_LIST=$(mktemp)
trap 'rm -f "$TASK_LIST"' EXIT
find "$DATASET_DIR" -mindepth 1 -maxdepth 1 -type d | sort > "$TASK_LIST"
n_tasks=$(wc -l < "$TASK_LIST" | tr -d ' ')
echo "==> $n_tasks task dir(s) in $DATASET_DIR"

# Warm the base layer once, serially. Without this, N parallel builds each push
# the ubuntu base concurrently on a cold registry.
first_task=$(head -1 "$TASK_LIST")
if [ -n "$first_task" ] && [ -n "$(image_of "$first_task")" ]; then
    echo "==> warming base layer via $(basename "$first_task")"
    build_one "$first_task"
fi

echo "==> building with parallelism $PARALLELISM (platform $PLATFORM)"
set +e
xargs -P "$PARALLELISM" -I{} bash -c 'build_one "$@"' _ {} < "$TASK_LIST" \
    | tee "$LOG_FILE"
rc=${PIPESTATUS[0]}
set -e

pushed=$(grep -c '^pushed' "$LOG_FILE" || true)
exists=$(grep -c '^exists' "$LOG_FILE" || true)
failed=$(grep -c '^FAILED' "$LOG_FILE" || true)
echo "=== pushed $pushed, already present $exists, failed $failed (log: $LOG_FILE) ==="
if [ "$rc" -ne 0 ] || [ "$failed" -ne 0 ]; then
    echo "=== re-run this script to retry only the failures ===" >&2
    exit 1
fi
