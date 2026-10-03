#!/usr/bin/env python3
"""Splice the original question into aggregated annotation files, matched by id.

The aggregation step carries only what the annotation is about -- id, model,
spans, summary -- but consumers generally also want the problem the trajectory was
reasoning about. That text already exists per task in the dataset's
trajectory.json, so this copies it across rather than re-deriving anything.

Files are rewritten in place with ``question`` inserted after ``model``, and the
write is atomic (temp file + replace) so an interrupted run cannot truncate an
annotation. Re-running is safe: a file that already carries the same question is
left untouched.

Usage:
  ./scripts/add_question.py \
      --labels  /path/to/aggregated \
      --dataset datasets/bespoke-stratos-traj-analysis

  # --dataset may be repeated; the first directory holding the id wins
  ./scripts/add_question.py --labels ... --dataset A --dataset B --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

# Output key order. `question` sits next to the other scalars rather than after
# the bulky spans list, so the head of each file stays readable.
FIELD_ORDER = ("id", "model", "question", "spans", "summary")


def find_question(task_id: str, datasets: list[Path]) -> str | None:
    """Return the question for *task_id* from the first dataset that has it."""
    for dataset in datasets:
        traj = dataset / task_id / "environment" / "trajectory.json"
        if not traj.exists():
            continue
        try:
            question = json.loads(traj.read_text()).get("question")
        except Exception:
            continue
        if question:
            return question
    return None


def write_atomic(path: Path, payload: dict) -> None:
    """Replace *path* only once the new content is fully on disk."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", required=True, help="Directory of {id}.json")
    parser.add_argument(
        "--dataset",
        required=True,
        action="append",
        help="Dataset directory of task dirs; repeatable, first match wins",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would change only"
    )
    args = parser.parse_args()

    labels = Path(args.labels)
    datasets = [Path(d) for d in args.dataset]
    for dataset in datasets:
        if not dataset.is_dir():
            raise SystemExit(f"dataset not found: {dataset}")

    added = unchanged = replaced = missing = broken = 0
    missing_ids: list[str] = []

    for path in sorted(labels.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except Exception as e:
            broken += 1
            print(f"  {path.name}: unreadable ({type(e).__name__})")
            continue

        # Trust the id inside the file, falling back to the filename.
        task_id = data.get("id") or path.stem
        question = find_question(task_id, datasets)
        if question is None:
            missing += 1
            missing_ids.append(task_id)
            continue

        existing = data.get("question")
        if existing == question:
            unchanged += 1
            continue

        data["question"] = question
        ordered = {k: data[k] for k in FIELD_ORDER if k in data}
        # Preserve anything a future step may have added.
        ordered.update({k: v for k, v in data.items() if k not in ordered})

        if not args.dry_run:
            write_atomic(path, ordered)
        if existing is None:
            added += 1
        else:
            replaced += 1

    prefix = "would add" if args.dry_run else "added"
    print(f"{prefix:<16}: {added}")
    if replaced:
        print(f"{'differed':<16}: {replaced} (question replaced)")
    print(f"{'already present':<16}: {unchanged}")
    print(f"{'no question found':<16}: {missing}")
    if missing_ids:
        print(f"  e.g. {missing_ids[:5]}")
    if broken:
        print(f"{'unreadable':<16}: {broken}")


if __name__ == "__main__":
    main()
