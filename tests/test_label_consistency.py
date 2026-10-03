"""The eight label names are written out in four places; assert they agree.

There is no single source of truth for the label set, and making one would mean
templating the instruction's markdown table (which carries a prose description per
label) and injecting the set into the generated verifier. The set is small and
stable, so the cheaper guard is this test: it re-derives the names from each place
independently and fails on any drift.

The failure it exists to catch is silent: if the instruction offered a label the
verifier did not accept, every trial that used that label would score 0 with a
plausible-looking "not one of the eight" message, and the loss would look like
model error rather than a config bug.

Sources checked:
  1. ``LABELS`` in adapter.py
  2. the markdown table inside the instruction that adapter.py renders
  3. ``LABELS`` in the python block of task-template/tests/test.sh
  4. ``LABELS`` in scripts/aggregate_labels.py

Nothing is imported: adapter.py pulls in ``datasets`` at module scope, and the
verifier is a shell script. Everything here is read with ``ast``/regex instead, so
the test runs anywhere with a bare interpreter.

Run standalone:
    python3 tests/test_label_consistency.py
or under pytest:
    uv run pytest tests/
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ADAPTER_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PY = ADAPTER_ROOT / "src/bespoke_stratos_traj_analysis/adapter.py"
TEST_SH = ADAPTER_ROOT / "src/bespoke_stratos_traj_analysis/task-template/tests/test.sh"
AGGREGATE_PY = ADAPTER_ROOT / "scripts/aggregate_labels.py"

# Spelled-out counts, so the instruction's "these eight labels" cannot go stale
# without this test noticing.
_NUMBER_WORDS = {
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
}


def _labels_from_module(source: str, origin: str) -> set[str]:
    """Read a module-level ``LABELS`` literal out of python source."""
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "LABELS" for t in node.targets):
            continue
        return set(ast.literal_eval(node.value))
    raise AssertionError(f"no module-level LABELS assignment in {origin}")


def _labels_from_verifier(source: str) -> set[str]:
    """Read ``LABELS`` out of the python heredoc embedded in test.sh."""
    lines = source.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("python3 - <<")]
    if not starts:
        raise AssertionError("no embedded python block in test.sh")
    start = starts[0] + 1
    end = next(i for i, line in enumerate(lines) if i > start and line.strip() == "PY")
    return _labels_from_module("\n".join(lines[start:end]), "test.sh")


def _labels_from_instruction(source: str) -> set[str]:
    """Read the label column of the instruction's markdown table."""
    # Rows look like: | `restating_problem` | Restating the problem ... |
    return set(re.findall(r"^\|\s*`([a-z_]+)`\s*\|", source, re.MULTILINE))


def _sources() -> dict[str, set[str]]:
    adapter_src = ADAPTER_PY.read_text()
    return {
        "adapter.py LABELS": _labels_from_module(adapter_src, "adapter.py"),
        "instruction table": _labels_from_instruction(adapter_src),
        "test.sh LABELS": _labels_from_verifier(TEST_SH.read_text()),
        "aggregate_labels.py LABELS": _labels_from_module(
            AGGREGATE_PY.read_text(), "aggregate_labels.py"
        ),
    }


def test_label_sets_agree() -> None:
    sources = _sources()
    reference_name, reference = next(iter(sources.items()))
    for name, labels in sources.items():
        assert labels, f"{name} yielded no labels"
        assert labels == reference, (
            f"{name} disagrees with {reference_name}: "
            f"only in {name}: {sorted(labels - reference)}, "
            f"only in {reference_name}: {sorted(reference - labels)}"
        )


def test_instruction_states_the_right_count() -> None:
    """The instruction says "these <word> labels"; keep the word truthful."""
    labels = _sources()["adapter.py LABELS"]
    word = _NUMBER_WORDS.get(len(labels))
    assert word, f"add {len(labels)} to _NUMBER_WORDS"
    source = ADAPTER_PY.read_text()
    assert f"these {word} labels" in source, (
        f"there are {len(labels)} labels, so the instruction should say "
        f'"these {word} labels"'
    )


if __name__ == "__main__":
    for name, labels in _sources().items():
        print(f"{name:<30} {len(labels)} labels")
    test_label_sets_agree()
    test_instruction_states_the_right_count()
    print("\nall four sources agree")
