#!/usr/bin/env bash
# Build and push the ONE image that holds every task's trajectory.
#
# Why one image instead of one per task: a first push of a new image uploads the
# whole ~29MB Ubuntu base layer. Measured against gitlab-registry.nrp-nautilus.io
# that is ~110s per image, and it does not go away with parallelism (the uplink
# saturates) or by putting all tags in one repository (buildx recompresses layers,
# so digests differ and the registry cannot dedupe). 6000 per-task images would be
# ~174GB and ~183h of uploading. This is one push of ~30MB.
#
# The image carries /trajectories/<task-name>.json for every task; each pod copies
# its own to /trajectory.json at startup via the command override in
# configs/nrp.yaml.
#
# Usage:
#   ./scripts/build_and_push_shared_image.sh <shared-context-dir> <image-ref> [platform]
#
# Example:
#   ./scripts/build_and_push_shared_image.sh \
#       datasets/bespoke-stratos-traj-analysis-shared-image \
#       gitlab-registry.nrp-nautilus.io/lucas_sjy/harbor_bespoke/bespoke-stratos:all
set -euo pipefail

CONTEXT_DIR="${1:?usage: build_and_push_shared_image.sh <shared-context-dir> <image-ref> [platform]}"
IMAGE_REF="${2:?missing <image-ref> -- must equal the --shared-image passed to the adapter}"
PLATFORM="${3:-linux/amd64}"

if [ ! -f "$CONTEXT_DIR/Dockerfile" ]; then
    echo "error: no Dockerfile in $CONTEXT_DIR" >&2
    echo "       generate tasks with --shared-image first" >&2
    exit 1
fi

n_traj=$(find "$CONTEXT_DIR/trajectories" -name '*.json' -type f | wc -l | tr -d ' ')
if [ "$n_traj" -eq 0 ]; then
    echo "error: $CONTEXT_DIR/trajectories is empty" >&2
    exit 1
fi

echo "==> $n_traj trajectory file(s), $(du -sh "$CONTEXT_DIR/trajectories" | cut -f1) uncompressed"
echo "==> building $IMAGE_REF for $PLATFORM"

# Not quiet: this is a single long upload and the progress output is the only
# feedback that it is alive.
docker buildx build --platform "$PLATFORM" --tag "$IMAGE_REF" --push "$CONTEXT_DIR"

echo "==> pushed $IMAGE_REF"
echo "==> verifying the image can select a trajectory the way a pod will"

# Same command the pod runs, with HB_TASK supplied manually instead of by the
# Downward API. Uses the host platform so it can actually execute here.
sample=$(find "$CONTEXT_DIR/trajectories" -name '*.json' -type f | head -1)
sample_task=$(basename "$sample" .json)
docker buildx build --tag hb-shared-selftest:local --load "$CONTEXT_DIR" >/dev/null 2>&1
if docker run --rm -e HB_TASK="$sample_task" hb-shared-selftest:local sh -c \
    'cp "/trajectories/$HB_TASK.json" /trajectory.json || { echo "no trajectory for $HB_TASK" >&2; exit 1; }; head -c 60 /trajectory.json'; then
    echo ""
    echo "=== ok: $IMAGE_REF pushed, trajectory selection verified for $sample_task ==="
else
    echo "=== FAILED: image pushed but trajectory selection did not work ===" >&2
    exit 1
fi
