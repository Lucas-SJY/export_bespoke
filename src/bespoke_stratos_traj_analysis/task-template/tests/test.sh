#!/bin/bash
# Verifier: reward=1 only when /output/labels.json is a well-formed segmentation
# of the trajectory.
#
# The previous version only checked that the file was non-empty and mentioned the
# source model, which passed unparseable files, JSON arrays, and outputs whose
# self-reported counts disagreed with their own spans. Because every such trial
# scored 1, the retry flow could not tell a good sample from a bad one.
#
# Checked here: valid JSON object; id and model match the trajectory; spans is a
# non-empty list of {start, end, label}; every label is one of the eight; and the
# ranges tile segment indices 0..N-1 exactly -- ordered, no gaps, no overlaps.
mkdir -p /logs/verifier
reward=0

python3 - <<'PY' > /logs/verifier/validation.txt 2>&1
import json
import sys

LABELS = {
    "restating_problem", "planning_next_step", "logical_deduction", "reflecting",
    "verifying", "correcting_itself", "recalling_knowledge", "concluding",
}

def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)

try:
    traj = json.load(open("/trajectory.json"))
except Exception as e:
    fail(f"cannot read /trajectory.json: {e}")

n = len(traj.get("segments") or [])
if n == 0:
    fail("trajectory has no segments")

try:
    with open("/output/labels.json") as fh:
        out = json.load(fh)
except FileNotFoundError:
    fail("/output/labels.json does not exist")
except Exception as e:
    fail(f"/output/labels.json is not valid JSON: {e}")

if not isinstance(out, dict):
    fail(f"top level is {type(out).__name__}, expected an object")
if out.get("id") != traj["id"]:
    fail(f"id is {out.get('id')!r}, expected {traj['id']!r}")
if out.get("model") != traj["model"]:
    fail(f"model is {out.get('model')!r}, expected {traj['model']!r}")

spans = out.get("spans")
if not isinstance(spans, list) or not spans:
    fail("spans must be a non-empty list")

cursor = 0
for pos, span in enumerate(spans):
    if not isinstance(span, dict):
        fail(f"span {pos} is {type(span).__name__}, expected an object")
    start, end, label = span.get("start"), span.get("end"), span.get("label")
    if not isinstance(start, int) or isinstance(start, bool):
        fail(f"span {pos}: start must be an integer, got {start!r}")
    if not isinstance(end, int) or isinstance(end, bool):
        fail(f"span {pos}: end must be an integer, got {end!r}")
    if label not in LABELS:
        fail(f"span {pos}: label {label!r} is not one of the eight")
    if start > end:
        fail(f"span {pos}: start {start} > end {end}")
    if start != cursor:
        fail(f"span {pos}: starts at {start}, expected {cursor} (gap or overlap)")
    cursor = end + 1

if cursor != n:
    fail(f"spans cover 0..{cursor - 1}, expected 0..{n - 1}")

print(f"OK: {len(spans)} span(s) tiling {n} segment(s)")
PY

if [ $? -eq 0 ]; then
    reward=1
fi

cat /logs/verifier/validation.txt
echo "$reward" > /logs/verifier/reward.txt
