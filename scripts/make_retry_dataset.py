#!/usr/bin/env python3
"""Build a dataset directory containing only the tasks that still need a label.

Lowering n_concurrent_trials (or changing anything else in the job config) makes
the previous job directory unresumable -- Harbor compares the stored config.json
and lock.json and refuses on any difference. Re-running the original 6000-task
config from scratch would redo thousands of tasks that already produced a good
label. Instead, point a second job config at the subset this script produces.

A task is "still needed" when the collected labels directory has no entry for it,
or the entry is not a usable annotation. The verifier only checks that the file is
non-empty and mentions the source model, so files that are unparseable, are JSON
arrays, or lack the eight summary labels do pass the trial but are useless.

Task directories are copied, not symlinked, so the retry dataset is independent of
the original. Names are preserved: the shared image keys trajectories by task name
and the pod reads its own `environment` label to pick one, so a renamed task would
find no trajectory.

Usage:
  ./scripts/make_retry_dataset.py \
      --source datasets/bespoke-stratos-traj-analysis \
      --labels output/labels \
      --dest   datasets/bespoke-stratos-retry
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

REQUIRED_LABELS = {
    "restating_problem",
    "planning_next_step",
    "logical_deduction",
    "reflecting",
    "verifying",
    "correcting_itself",
    "recalling_knowledge",
    "concluding",
}


def is_usable(path: Path) -> bool:
    """True when *path* holds an annotation worth keeping."""
    try:
        data = json.loads(path.read_text())
    except Exception:
        return False
    return (
        isinstance(data, dict)
        and data.get("model")
        and bool(data.get("spans"))
        and set(data.get("summary") or {}) >= REQUIRED_LABELS
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Full dataset directory")
    parser.add_argument("--labels", required=True, help="Collected {id}.json directory")
    parser.add_argument("--dest", required=True, help="Retry dataset directory")
    parser.add_argument(
        "--overwrite", action="store_true", help="Replace an existing --dest"
    )
    args = parser.parse_args()

    source, labels, dest = Path(args.source), Path(args.labels), Path(args.dest)
    if not source.is_dir():
        raise SystemExit(f"source not found: {source}")

    if dest.exists():
        if not args.overwrite:
            raise SystemExit(f"{dest} exists; pass --overwrite to replace it")
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    done = unusable = copied = 0
    for task_dir in sorted(p for p in source.iterdir() if p.is_dir()):
        label = labels / f"{task_dir.name}.json"
        if label.exists():
            if is_usable(label):
                done += 1
                continue
            unusable += 1
        shutil.copytree(task_dir, dest / task_dir.name)
        copied += 1

    print(f"source tasks      : {done + copied}")
    print(f"already labelled  : {done}")
    print(f"label unusable    : {unusable} (re-queued)")
    print(f"copied to retry   : {copied}")
    print(f"\nretry dataset: {dest}")


if __name__ == "__main__":
    main()
