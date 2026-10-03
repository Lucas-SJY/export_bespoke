#!/usr/bin/env python3
"""Expand index-range annotations into full ones, computing counts offline.

The agent emits only `{start, end, label}` ranges. Span text, word counts, and the
per-label summary are derived here from the trajectory's `segments`, because they
are pure bookkeeping: asking the model to do the arithmetic produced thousands of
totals that disagreed with the model's own spans, and none of it was information
only the model had.

Input  : a job directory of trials, plus the dataset holding /trajectory.json data
Output : one {id}.json per trial, in the shape downstream consumers expect --
         spans carrying text and token_count, plus a summary over the eight labels

Usage:
  ./scripts/aggregate_labels.py \
      --job     jobs/bespoke-stratos-v2 \
      --dataset datasets/bespoke-stratos-traj-analysis \
      --dest    output/labels
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

LABELS = (
    "restating_problem",
    "planning_next_step",
    "logical_deduction",
    "reflecting",
    "verifying",
    "correcting_itself",
    "recalling_knowledge",
    "concluding",
)


def expand(raw: dict, segments: list[dict]) -> dict:
    """Turn index ranges into text-carrying spans plus a summary."""
    spans = []
    for span in raw["spans"]:
        texts = [segments[i]["text"] for i in range(span["start"], span["end"] + 1)]
        text = " ".join(texts)
        spans.append(
            {
                "start": span["start"],
                "end": span["end"],
                "label": span["label"],
                "text": text,
                "token_count": len(text.split()),
            }
        )

    total = sum(s["token_count"] for s in spans)
    summary: dict[str, object] = {"total_token_count": total}
    for label in LABELS:
        count = sum(s["token_count"] for s in spans if s["label"] == label)
        summary[label] = {
            "token_count": count,
            # Exact by construction: the parts are counted from the same spans as
            # the total, so these always sum to 100 (or to 0 for an empty trace).
            "percentage": round(count / total * 100, 2) if total else 0.0,
        }

    return {
        "id": raw["id"],
        "model": raw["model"],
        "spans": spans,
        "summary": summary,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, help="Job directory with trial dirs")
    parser.add_argument("--dataset", required=True, help="Dataset with task dirs")
    parser.add_argument("--dest", required=True, help="Where to write {id}.json")
    args = parser.parse_args()

    job, dataset, dest = Path(args.job), Path(args.dataset), Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)

    written = skipped = failed = rejected = 0
    for src in sorted(job.glob("sample_*/artifacts/output/labels.json")):
        task = src.parts[-4].split("__")[0]

        # Only expand what the verifier accepted. Expansion tolerates spans that
        # tests/test.sh rejects -- a run that stops one segment short still
        # expands cleanly -- so without this check a malformed annotation lands in
        # the output looking exactly like a good one.
        reward = src.parents[2] / "verifier" / "reward.txt"
        if not reward.exists() or reward.read_text().strip() != "1":
            rejected += 1
            continue

        traj_path = dataset / task / "environment" / "trajectory.json"
        if not traj_path.exists():
            failed += 1
            print(f"  {task}: no trajectory.json in dataset")
            continue
        try:
            raw = json.loads(src.read_text())
            segments = json.loads(traj_path.read_text())["segments"]
            result = expand(raw, segments)
        except Exception as e:
            failed += 1
            print(f"  {task}: {type(e).__name__}: {e}")
            continue

        out = dest / f"{result['id']}.json"
        if out.exists():
            skipped += 1
            continue
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        written += 1

    print(f"written : {written}")
    print(f"skipped : {skipped} (already present)")
    print(f"rejected: {rejected} (reward != 1)")
    print(f"failed  : {failed}")
    print(f"total in {dest}: {len(list(dest.glob('*.json')))}")


if __name__ == "__main__":
    main()
