#!/usr/bin/env python3
"""View an annotated sample next to the raw Bespoke-Stratos-17k row it came from.

Annotation ids are positional: `sample_NNNNNN` is row NNNNNN of the HF train
split, because the adapter numbered records with `enumerate` over the stream and
let skipped records keep their slot. Bespoke-Stratos-17k carries no id column of
its own, so row order is the only join key -- which makes it worth pinning the
revision, and worth checking the pairing rather than trusting it.

This script does both: it prints the raw row beside the annotation, and verifies
that the annotation's `question` is the row's user turn and that its spans,
concatenated, reproduce the assistant's <|begin_of_thought|> block.

Examples:
    uv run python scripts/view_annotation.py                      # a random annotated sample
    uv run python scripts/view_annotation.py --id sample_002262
    uv run python scripts/view_annotation.py --index 2262 --full  # no truncation
    uv run python scripts/view_annotation.py --check              # verify the whole collection
    uv run python scripts/view_annotation.py --stats              # coverage only, no download

Needs the `datasets` package, which the setup_bespoke project provides:
    uv run python scripts/view_annotation.py --id sample_000000
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

DATASET = "bespokelabs/Bespoke-Stratos-17k"

# The revision the annotations were generated against. Row order is the join key,
# so a different revision can silently shift every id.
REVISION = "9e9adba943911a9fc44dffcb30aaa18dc96ae6df"

# setup_bespoke/output/labels -- this file lives in setup_bespoke/scripts/.
DEFAULT_LABELS = Path(__file__).resolve().parents[1] / "output/labels"

THOUGHT_RE = re.compile(r"<\|begin_of_thought\|>(.*?)<\|end_of_thought\|>", re.DOTALL)
SOLUTION_RE = re.compile(
    r"<\|begin_of_solution\|>(.*?)<\|end_of_solution\|>", re.DOTALL
)
USER_ROLES = ("user", "human")
ASSISTANT_ROLES = ("assistant", "gpt")

BAR = "=" * 78
DASH = "-" * 78


def norm(text: str) -> str:
    """Whitespace-insensitive form, for comparing text that was re-joined."""
    return re.sub(r"\s+", " ", text).strip()


def turn(conversations, roles):
    for entry in conversations:
        if entry.get("from") in roles:
            return entry.get("value") or ""
    return ""


def clip(text: str, limit: int | None) -> str:
    if limit is None or len(text) <= limit:
        return text
    return text[:limit] + f" ... [+{len(text) - limit} chars]"


def load_rows(revision: str):
    try:
        from datasets import load_dataset
    except ImportError:
        sys.exit(
            "error: the `datasets` package is required.\n"
            "       run it inside the setup_bespoke project instead:\n"
            f"       uv run python scripts/{Path(__file__).name} ..."
        )
    return load_dataset(DATASET, split="train", revision=revision)


def index_of(label_id: str) -> int:
    m = re.fullmatch(r"sample_(\d+)", label_id)
    if not m:
        sys.exit(f"error: id must look like sample_000123, got {label_id!r}")
    return int(m.group(1))


def compare(ann: dict, row: dict) -> list[tuple[str, bool, str]]:
    """Check the annotation against the raw row. Returns (name, ok, detail)."""
    conversations = row.get("conversations") or []
    question = turn(conversations, USER_ROLES)
    assistant = turn(conversations, ASSISTANT_ROLES)
    thought_match = THOUGHT_RE.search(assistant)
    thought = thought_match.group(1).strip() if thought_match else ""

    checks = []

    ok = ann.get("question") == question
    checks.append(
        (
            "question is byte-identical to the raw user turn",
            ok,
            ""
            if ok
            else f"annotation {len(ann.get('question') or '')} chars / raw {len(question)} chars",
        )
    )

    joined = " ".join(s["text"] for s in ann.get("spans", []))
    ok = bool(thought) and norm(joined) == norm(thought)
    checks.append(
        (
            "spans concatenate to the raw thought block (whitespace-insensitive)",
            ok,
            ""
            if ok
            else f"annotation {len(norm(joined))} chars / raw {len(norm(thought))} chars",
        )
    )

    spans = ann.get("spans", [])
    cursor, gapless = 0, True
    for span in spans:
        if span["start"] != cursor:
            gapless = False
            break
        cursor = span["end"] + 1
    checks.append(
        (
            "spans tile the segments with no gap or overlap",
            gapless,
            "" if gapless else f"breaks at index {cursor}",
        )
    )

    summary = ann.get("summary") or {}
    total = summary.get("total_token_count", 0)
    parts = sum(
        v["token_count"] for k, v in summary.items() if k != "total_token_count"
    )
    ok = parts == total
    checks.append(
        (
            "per-label counts sum to summary total",
            ok,
            "" if ok else f"{parts} != {total}",
        )
    )

    return checks


def show(ann: dict, row: dict, limit: int | None) -> None:
    conversations = row.get("conversations") or []
    question = turn(conversations, USER_ROLES)
    assistant = turn(conversations, ASSISTANT_ROLES)
    thought_match = THOUGHT_RE.search(assistant)
    solution_match = SOLUTION_RE.search(assistant)

    print(BAR)
    print(f"  {ann['id']}   (row {index_of(ann['id'])} of the HF train split)")
    print(BAR)

    print("\n[RAW DATASET] as downloaded from HuggingFace")
    print(DASH)
    print(f"columns      : {sorted(row.keys())}")
    print(f"system       : {clip(row.get('system') or '', 200)}")
    print(
        f"conversations: {len(conversations)} turns "
        f"({', '.join(c.get('from', '?') for c in conversations)})"
    )
    print(f"\n[user turn] {len(question)} chars")
    print(clip(question, limit))
    print(f"\n[assistant turn] {len(assistant)} chars")
    if thought_match:
        print(f"  <|begin_of_thought|> {len(thought_match.group(1).strip())} chars")
        print("  " + clip(thought_match.group(1).strip(), limit).replace("\n", "\n  "))
    if solution_match:
        print(f"\n  <|begin_of_solution|> {len(solution_match.group(1).strip())} chars")
        print("  " + clip(solution_match.group(1).strip(), 300).replace("\n", "\n  "))

    print("\n[ANNOTATION]")
    print(DASH)
    print(f"fields  : {list(ann.keys())}")
    print(f"model   : {ann.get('model')}")
    print(f"question: {clip(ann.get('question') or '', 160)}")
    spans = ann.get("spans", [])
    covered = spans[-1]["end"] + 1 if spans else 0
    print(f"spans   : {len(spans)}, covering segments 0..{covered - 1}")
    print()
    for span in spans:
        print(
            f"  [{span['start']:>4}..{span['end']:<4}] {span['label']:<20}"
            f" {span['token_count']:>5}w"
        )
        print(
            "      "
            + clip(span["text"], limit if limit else None).replace("\n", "\n      ")
        )
    summary = ann.get("summary") or {}
    print(f"\nsummary : total={summary.get('total_token_count')} words")
    for name, value in summary.items():
        if name == "total_token_count" or not value["token_count"]:
            continue
        print(f"  {name:<22} {value['token_count']:>6}w  {value['percentage']:>6.2f}%")

    print("\n[PAIRING CHECKS]")
    print(DASH)
    for name, ok, detail in compare(ann, row):
        print(
            f"  {'PASS' if ok else 'FAIL'}  {name}"
            + (f"   [{detail}]" if detail else "")
        )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--labels", default=str(DEFAULT_LABELS), help="directory of annotation files"
    )
    parser.add_argument("--id", help="e.g. sample_002262")
    parser.add_argument("--index", type=int, help="HF row number; same as --id")
    parser.add_argument("--revision", default=REVISION, help="HF dataset revision")
    parser.add_argument("--full", action="store_true", help="do not truncate long text")
    parser.add_argument(
        "--check", action="store_true", help="verify every annotation and summarize"
    )
    parser.add_argument(
        "--stats", action="store_true", help="coverage only; does not load the dataset"
    )
    args = parser.parse_args()

    labels = Path(args.labels)
    if not labels.is_dir():
        sys.exit(f"error: annotation directory not found: {labels}")
    files = {p.stem: p for p in labels.glob("sample_*.json")}
    if not files:
        sys.exit(f"error: no sample_*.json under {labels}")

    if args.stats:
        idx = sorted(index_of(name) for name in files)
        span = idx[-1] - idx[0] + 1
        print(f"annotations : {len(idx)}")
        print(f"id range    : sample_{idx[0]:06d} ~ sample_{idx[-1]:06d}")
        print(f"rows spanned: {span}")
        print(f"coverage    : {len(idx)}/{span} = {len(idx) / span * 100:.1f}%")
        print(f"gaps        : {span - len(idx)} rows without an annotation")
        print(f"dataset     : {DATASET} @ {args.revision[:12]}")
        return

    rows = load_rows(args.revision)
    print(f"loaded {DATASET} @ {args.revision[:12]}  ({len(rows)} rows)\n")

    if args.check:
        failed = []
        for name in sorted(files):
            ann = json.loads(files[name].read_text())
            i = index_of(name)
            if i >= len(rows):
                failed.append((name, "row number is past the end of the dataset"))
                continue
            bad = [n for n, ok, _ in compare(ann, rows[i]) if not ok]
            if bad:
                failed.append((name, "; ".join(bad)))
        print(
            f"checked {len(files)} annotations: "
            f"{len(files) - len(failed)} passed, {len(failed)} failed"
        )
        for name, why in failed[:20]:
            print(f"  {name}: {why}")
        sys.exit(1 if failed else 0)

    if args.index is not None:
        target = f"sample_{args.index:06d}"
    elif args.id:
        target = args.id
    else:
        target = random.choice(sorted(files))
        print(f"(no --id given, picked {target} at random)\n")

    if target not in files:
        i = index_of(target)
        sys.exit(
            f"error: no annotation for {target}. That row "
            f"{'exists' if i < len(rows) else 'does not exist'} in the raw dataset, "
            "but the trial failed or the row was outside the generated range "
            "(rows are only annotated once a pass has been generated and run for them)."
        )

    show(
        json.loads(files[target].read_text()),
        rows[index_of(target)],
        None if args.full else 400,
    )


if __name__ == "__main__":
    main()
