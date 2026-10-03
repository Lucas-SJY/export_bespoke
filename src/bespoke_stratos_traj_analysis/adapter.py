"""Converts bespokelabs/Bespoke-Stratos-17k records into Harbor task directories."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Iterable

from datasets import load_dataset

DATASET_NAME = "bespokelabs/Bespoke-Stratos-17k"
VALID_SPLITS = ("train",)

# Bespoke-Stratos-17k has no per-row source-model column: the whole dataset is
# distilled from DeepSeek-R1, so the source model is a constant (overridable via
# --source-model).
DEFAULT_SOURCE_MODEL = "DeepSeek-R1"

# Image repository prefix used when --registry is passed. Each task bakes its own
# trajectory.json, so every task needs its own image.
IMAGE_PREFIX = "bespoke-stratos-traj-analysis"

# Dockerfile for the shared image built by --shared-image: one image holding every
# task's trajectory under /trajectories/<task-name>.json. Pods select their own at
# startup (see the pod_overrides command in the NRP job config), so no per-task
# image is needed.
#
# This exists because per-task images are bandwidth-infeasible: a first push of a
# new image uploads the whole ~29MB base layer, measured at ~110s against
# gitlab-registry.nrp-nautilus.io, and neither parallelism (the link saturates) nor
# sharing one repository across tags (buildx recompresses, so digests differ and
# the registry cannot dedupe) avoids it. 6000 tasks would mean ~174GB and ~183h of
# uploading; the shared image is one push of ~30MB.
_SHARED_DOCKERFILE = """\
FROM ubuntu:24.04

# tests/test.sh validates the span intervals in Python, so the image needs a
# real interpreter. NOT python3-minimal: that package ships without the
# standard library, so `import json` fails and every trial scores 0 on a
# verifier crash rather than on its own output.
RUN apt-get update \\
    && apt-get install -y --no-install-recommends python3 \\
    && rm -rf /var/lib/apt/lists/* \\
    && mkdir -p /output /logs

# Every task's trajectory. The pod's start command copies the one named by its own
# `environment` label to /trajectory.json, which is where the instruction says it is.
COPY trajectories /trajectories

WORKDIR /workspace
"""

_USER_ROLES = ("user", "human")
_ASSISTANT_ROLES = ("assistant", "gpt")

_THOUGHT_RE = re.compile(r"<\|begin_of_thought\|>(.*?)<\|end_of_thought\|>", re.DOTALL)
_SOLUTION_RE = re.compile(
    r"<\|begin_of_solution\|>(.*?)<\|end_of_solution\|>", re.DOTALL
)

# Paragraph break, then sentence-final punctuation followed by whitespace.
_PARA_SPLIT_RE = re.compile(r"\n\s*\n")
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# The eight behavioural labels. Kept here so the adapter, the instruction, and the
# verifier it writes all agree on one list.
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


def segment_thought(text: str) -> list[str]:
    """Split a reasoning trace into numbered sentences.

    The agent labels index ranges over these segments instead of echoing the text
    back, so this split is the contract: it must be deterministic, and the same
    function must produce the same segments for the offline aggregation step.
    """
    segments: list[str] = []
    for paragraph in _PARA_SPLIT_RE.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        for sentence in _SENT_SPLIT_RE.split(paragraph):
            sentence = sentence.strip()
            if sentence:
                segments.append(sentence)
    return segments


_TEMPLATE_DIR = Path(__file__).parent / "task-template"


def _sanitize_id(sample_id: str) -> str:
    """Make a sample id safe for use as a directory name."""
    return re.sub(r"[^\w.-]", "_", sample_id)


def _image_repository(task_name: str) -> str:
    """Docker repository name (no registry, no tag) for a task."""
    return f"{IMAGE_PREFIX}-{task_name}".lower().replace("_", "-")


def _first_turn(conversations: list[dict], roles: tuple[str, ...]) -> str | None:
    for turn in conversations:
        if turn.get("from") in roles:
            return turn.get("value")
    return None


def _extract(regex: re.Pattern, text: str) -> str | None:
    match = regex.search(text)
    return match.group(1).strip() if match else None


def _build_instruction(record: dict, sample_id: str, n_segments: int) -> str:
    """Build the span-labeling instruction for a pre-segmented trajectory."""
    model = record.get("model", "unknown")
    last = n_segments - 1

    return f"""\
A model-generated reasoning trajectory has been placed at `/trajectory.json`.

## Trajectory Metadata

- **Sample ID**: `{sample_id}`
- **Source Model**: `{model}`
- **Segment count**: `{n_segments}` (indices `0` .. `{last}`)

The JSON object contains the original question (`question`), the model's
chain-of-thought already split into numbered sentences (`segments`), the model's
final solution (`solution`), and the source model identifier (`model`).

Each entry of `segments` is `{{"i": <index>, "text": <sentence>}}`.

## Label Definitions

Assign each segment one of these eight labels:

| Label | Description |
|-------|-------------|
| `restating_problem` | Restating the problem in its own words, extracting given values, conditions, or definitions from the question. |
| `planning_next_step` | Deciding on a solution strategy, outlining steps, decomposing the problem into subproblems. |
| `logical_deduction` | Carrying out forward reasoning: step-by-step derivations, computations, applying rules or formulas to produce new intermediate results. |
| `reflecting` | Spontaneously evaluating an intermediate result or noticing something odd, interesting, or worth attention (e.g. "Hmm...", "Wait, that seems..."). |
| `verifying` | Deliberately re-checking earlier work: re-computing steps, re-deriving results another way, running a systematic verification pass. |
| `correcting_itself` | Fixing a computational mistake, revising a wrong assumption or approach, backtracking after a dead end. |
| `recalling_knowledge` | Recalling external knowledge not stated in the problem: definitions, theorems, formulas, known facts, or related problems. |
| `concluding` | Stating the final answer, expressing final confidence, or formatting the output. |

Notes on distinctions:
- `reflecting` is a brief, spontaneous reaction to what was just derived; `verifying` is a deliberate, systematic re-check of earlier steps. A "Let me check my steps again" announcement starts a `verifying` span.
- `restating_problem` repeats what the problem already says; `recalling_knowledge` brings in outside knowledge.
- `logical_deduction` produces new results; `verifying` re-derives already-obtained results.

## Your Task

Read `/trajectory.json`. Group the `segments` into consecutive runs that share a
label, and write those runs to `/output/labels.json`:

```json
{{
  "id": "{sample_id}",
  "model": "{model}",
  "spans": [
    {{"start": 0, "end": 0, "label": "planning_next_step"}},
    {{"start": 1, "end": 3, "label": "restating_problem"}}
  ]
}}
```

`start` and `end` are segment indices and `end` is **inclusive**, so
`{{"start": 1, "end": 3}}` covers segments 1, 2 and 3.

Rules — a trial is scored 0 unless all of these hold:
- Spans are ordered and contiguous: the first starts at `0`, the last ends at `{last}`, and each span starts exactly one after the previous one ends. No gaps, no overlaps.
- `start` <= `end` for every span.
- Every `label` is one of the eight names above, spelled exactly.
- `id` and `model` match the values in `/trajectory.json`.
- `/output/labels.json` is the only file you write.

Do **not** copy segment text into your output, and do **not** compute token
counts, percentages, or any summary — index ranges and labels are the whole
deliverable, and everything else is derived from them afterwards.
"""


class BespokeStratosTrajAnalysisAdapter:
    """
    Converts bespokelabs/Bespoke-Stratos-17k records into Harbor task directories.

    Each record's assistant turn wraps its chain-of-thought in
    <|begin_of_thought|>...<|end_of_thought|>. The adapter extracts that thought
    as `thought_trace`, packages the trajectory into the Docker image at
    /trajectory.json, and the agent writes span-level labels of `thought_trace`
    to /output/labels.json (including the source model name).

    When `registry` is set, each task.toml also pins `[environment] docker_image`
    to a pre-built image in that registry. Kubernetes environments (`ack`) cannot
    build images without a privileged in-cluster Docker-in-Docker builder, which
    NRP does not allow, so images are built and pushed from the host first (see
    scripts/build_and_push_images.sh).
    """

    def __init__(
        self,
        output_dir: Path,
        split: str = "train",
        limit: int | None = None,
        source_model: str = DEFAULT_SOURCE_MODEL,
        overwrite: bool = False,
        hf_token: str | None = None,
        registry: str | None = None,
        image_tag: str = "latest",
        shared_image: str | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.split = split
        self.limit = limit
        self.source_model = source_model
        self.overwrite = overwrite
        self.hf_token = hf_token
        self.registry = registry.rstrip("/") if registry else None
        self.image_tag = image_tag
        self.shared_image = shared_image
        # Build context for the shared image, kept as a sibling of the dataset dir
        # so Harbor's task discovery never sees it as a task.
        self.shared_dir = (
            self.output_dir.parent / f"{self.output_dir.name}-shared-image"
            if shared_image
            else None
        )

    def _load_records(self) -> Iterable[dict]:
        kwargs: dict = {"split": self.split, "streaming": True}
        if self.hf_token:
            kwargs["token"] = self.hf_token
        ds = load_dataset(DATASET_NAME, **kwargs)
        if self.limit is not None:
            ds = ds.take(self.limit)
        return ds

    def _docker_image_line(self, task_name: str) -> str:
        if self.shared_image:
            # Every task points at the same image; the pod picks its trajectory.
            return f'docker_image = "{self.shared_image}"\n'
        if not self.registry:
            return ""
        image = f"{self.registry}/{_image_repository(task_name)}:{self.image_tag}"
        return f'docker_image = "{image}"\n'

    def _init_shared_context(self) -> None:
        """Create the shared image build context (Dockerfile + trajectories dir)."""
        if self.shared_dir is None:
            return
        if self.overwrite and self.shared_dir.exists():
            shutil.rmtree(self.shared_dir)
        (self.shared_dir / "trajectories").mkdir(parents=True, exist_ok=True)
        (self.shared_dir / "Dockerfile").write_text(_SHARED_DOCKERFILE)

    def generate_task(self, record: dict, index: int) -> Path | None:
        conversations = record.get("conversations") or []
        question = _first_turn(conversations, _USER_ROLES)
        assistant = _first_turn(conversations, _ASSISTANT_ROLES)
        if not assistant:
            return None

        thought_trace = _extract(_THOUGHT_RE, assistant)
        if not thought_trace:
            # No chain-of-thought to label.
            return None
        segments = segment_thought(thought_trace)
        if not segments:
            # Nothing to index; a task with zero segments has no valid answer.
            return None
        solution = _extract(_SOLUTION_RE, assistant)

        sample_id = f"sample_{index:06d}"
        task_name = _sanitize_id(sample_id)
        task_dir = self.output_dir / task_name

        if task_dir.exists():
            if not self.overwrite:
                return None
            shutil.rmtree(task_dir)

        task_dir.mkdir(parents=True)
        (task_dir / "environment").mkdir()
        (task_dir / "tests").mkdir()

        # instruction.md: short prompt, points agent to /trajectory.json
        trajectory = {
            "id": sample_id,
            "model": self.source_model,
            "question": question,
            "thought_trace": thought_trace,
            # The agent labels index ranges over these instead of echoing text, so
            # the numbering is the interface between instruction, verifier, and the
            # offline aggregation step.
            "segments": [{"i": i, "text": t} for i, t in enumerate(segments)],
            "solution": solution,
        }
        (task_dir / "instruction.md").write_text(
            _build_instruction(trajectory, sample_id, len(segments))
        )

        # task.toml
        toml_tpl = (_TEMPLATE_DIR / "task.toml").read_text()
        (task_dir / "task.toml").write_text(
            toml_tpl.replace("{task_name}", task_name).replace(
                "{docker_image_line}\n", self._docker_image_line(task_name)
            )
        )

        # trajectory JSON baked into the Docker build context
        trajectory_json = json.dumps(trajectory, ensure_ascii=False, indent=2)
        trajectory_path = task_dir / "environment" / "trajectory.json"
        trajectory_path.write_text(trajectory_json)

        # Same payload in the shared image's context, keyed by task name. The name
        # must match the pod's `environment` label, which ACKEnvironment derives
        # from the task's short name -- i.e. this task_name verbatim.
        if self.shared_dir is not None:
            (self.shared_dir / "trajectories" / f"{task_name}.json").write_text(
                trajectory_json
            )

        # Dockerfile: copies trajectory.json into the image
        dockerfile_tpl = (_TEMPLATE_DIR / "environment" / "Dockerfile").read_text()
        (task_dir / "environment" / "Dockerfile").write_text(dockerfile_tpl)

        # tests/test.sh
        dest = task_dir / "tests" / "test.sh"
        shutil.copy(_TEMPLATE_DIR / "tests" / "test.sh", dest)
        dest.chmod(0o755)

        return task_dir

    def run(self) -> tuple[int, int]:
        """Generate tasks. Returns (success_count, skip_count)."""
        self._init_shared_context()
        success = 0
        skipped = 0
        for index, record in enumerate(self._load_records()):
            result = self.generate_task(record, index)
            if result is None:
                skipped += 1
            else:
                success += 1
        return success, skipped
