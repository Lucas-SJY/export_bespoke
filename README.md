# setup_bespoke

Span-level labeling of the reasoning traces in
[bespokelabs/Bespoke-Stratos-17k](https://huggingface.co/datasets/bespokelabs/Bespoke-Stratos-17k),
run as [Harbor](https://github.com/harbor-framework/harbor) tasks on the NRP (Nautilus)
Kubernetes cluster.

Each task gives an agent one chain-of-thought, already split into numbered sentences
("segments"), and asks it to cover the segments with contiguous index ranges, each
tagged with one of eight labels:

| Label | Meaning |
|---|---|
| `restating_problem` | Restates the problem or extracts its givens |
| `planning_next_step` | Chooses a strategy or outlines steps |
| `logical_deduction` | Forward reasoning that produces new results |
| `reflecting` | A brief, spontaneous reaction ("Hmm, wait...") |
| `verifying` | A deliberate re-check of earlier work |
| `correcting_itself` | Fixes a mistake or backtracks |
| `recalling_knowledge` | Brings in outside facts, theorems, formulas |
| `concluding` | States the final answer |

The agent writes only `{"start", "end", "label"}` ranges. A verifier accepts the output
only if the ranges tile every segment exactly once, and an export step then turns the
ranges into the final annotation files, with the span text, word counts, and a
per-label summary filled in.

This directory holds everything that is specific to the dataset. It does not contain
Harbor itself; it runs against a separate Harbor checkout.

---

## Layout

```
setup_bespoke/
├── configs/
│   ├── nrp.yaml            # main NRP run (pass one)
│   ├── nrp-rest.yaml       # second pass: rows the first pass did not annotate
│   ├── nrp-retry.yaml      # retry a subset with different settings
│   └── local-docker.yaml   # local Docker run; only the model is remote
├── src/bespoke_stratos_traj_analysis/
│   ├── adapter.py          # HF row -> Harbor task directory
│   ├── main.py             # CLI: `bespoke-stratos-traj-analysis`
│   ├── opencode_watchdog.py  # OpenCode agent + idle watchdog + keepalive heartbeat
│   └── task-template/      # task.toml, Dockerfile, tests/test.sh (the verifier)
├── scripts/
│   ├── build_and_push_shared_image.sh  # one image holding every trajectory
│   ├── build_and_push_images.sh        # legacy: one image per task (see below)
│   ├── make_retry_dataset.py           # copy out the tasks that still lack a label
│   ├── aggregate_labels.py             # export step 1: ranges -> full annotations
│   ├── add_question.py                 # export step 2: attach the original question
│   ├── export.sh                       # runs both export steps in order
│   └── view_annotation.py              # show/verify an annotation against the HF row
├── tests/test_label_consistency.py     # the 8 label names agree in all 4 places
├── pyproject.toml / uv.lock            # deps for generation and the tools (`datasets`)
└── .gitignore                          # datasets/, jobs/, output/ are generated
```

Generated data lives under this directory too, and is git-ignored:
`datasets/` (tasks), `jobs/` (Harbor output), `output/` (exported annotations).

**Run every command from the `setup_bespoke/` root.** The configs use paths relative
to the directory Harbor runs from.

---

## Requirements

- **Harbor with the `ack` environment.** Tested with Harbor 0.20.0. Older releases
  (0.14.0, for example) do not have `ack` and reject these configs.
- **uv**, and **Python 3.12** available to it (see `UV_PYTHON` below).
- **Docker** with `buildx`, to build and push the task image.
- **kubectl** configured for NRP: kubeconfig from the NRP portal, the `nautilus`
  context, and [kubelogin](https://github.com/int128/kubelogin) for the OIDC login.
- An **NRP namespace** you can create pods in (`wang-research-lab` in the configs).
- An **NRP GitLab project** whose container registry holds the task image.
  Set the project's visibility to Public so pods can pull without a secret.
- An **OpenAI-compatible inference endpoint** and key (the NRP LLM gateway).

"First-time setup" below walks through getting each of these.

---

## First-time setup

Do this once on a new machine. Each step ends with a check. Do not move on until the
check passes, because a gap here shows up later as a confusing failure on the cluster.
Commands are for macOS with Homebrew. On Linux, install the same tools with your package
manager.

### Setup 1. Install the local tools

```bash
brew install uv kubectl tmux
brew install int128/kubelogin/kubelogin   # NRP's OIDC login plugin. Plain `brew install kubelogin` is a different (Azure) tool
```

Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and start it.

Check:

```bash
uv --version
kubectl version --client
kubectl oidc-login --version
docker info > /dev/null && echo "docker running"
tmux -V
```

### Setup 2. Get Harbor

Any Harbor that has the `ack` environment works. Upstream `main` has it (tested at 0.20.0).

```bash
git clone https://github.com/harbor-framework/harbor.git
cd harbor
UV_PYTHON=3.12 uv sync --all-extras       # --all-extras installs the kubernetes client that `ack` needs
```

Check:

```bash
UV_PYTHON=3.12 uv run python -V                                          # Python 3.12.x
UV_PYTHON=3.12 uv run python -c "import kubernetes, harbor.environments.ack; print('ok')"
```

Always pass `UV_PYTHON=3.12` when running anything in this checkout. The "Environment
variables" section explains why.

### Setup 3. Get NRP cluster access

1. Log in at <https://nrp.ai> with your institution through CILogon. This creates your
   account.
2. Get added to a namespace. Students ask their PI or the namespace admin. Faculty can
   request a namespace from [NRP support](https://nrp.ai/contact/).
3. Download the kubeconfig and select the context. The `curl` below **overwrites**
   `~/.kube/config`. If you already use other clusters, merge the file by hand instead.

   ```bash
   mkdir -p ~/.kube
   curl -fSL -o ~/.kube/config https://nrp.ai/config
   kubectl config use-context nautilus
   kubectl config set contexts.nautilus.namespace <your-namespace>
   ```

4. Ask the admins for your namespace's pod quota. Keep `n_concurrent_trials` below it.

Check. The first call opens a browser to log in:

```bash
kubectl get pods -n <your-namespace>      # a list, or "No resources found" -- not "Forbidden"
```

Then confirm you can actually start a pod. NRP's admission policy
(`container-must-have-limits-and-requests`) rejects containers that lack CPU and memory
requests and limits, so use this spec as written. A `--dry-run=server` is not a
substitute: on NRP it bypasses that policy and accepts specs the real request rejects.

```bash
kubectl apply -n <your-namespace> -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: probe-<your-name>
spec:
  restartPolicy: Never
  activeDeadlineSeconds: 300
  containers:
    - name: main
      image: ubuntu:24.04
      command: ["sleep", "120"]
      resources:
        requests: {cpu: "1", memory: 2Gi, ephemeral-storage: 1Gi}
        limits:   {cpu: "1", memory: 2Gi, ephemeral-storage: 1Gi}
EOF
kubectl wait -n <your-namespace> --for=condition=Ready pod/probe-<your-name> --timeout=120s
kubectl delete -n <your-namespace> pod probe-<your-name>
```

`condition met` means pods can be scheduled. If the pod stays `Pending` for minutes,
the quota is usually full; `kubectl describe pod` shows why.

### Setup 4. Set up the container registry

Skip this step if you will reuse an image that is already pushed. See "Handing it to
someone else".

1. Log in at <https://gitlab.nrp-nautilus.io> with the same CILogon account and create a
   blank project. Do not push code to it. Only its container registry is used.
2. **Settings → General → Visibility → Public**, so pods can pull without a secret.
3. **Deploy → Container Registry** shows the exact image prefix, for example
   `gitlab-registry.nrp-nautilus.io/<user>/<project>`. Copy it from there. It must be
   all lowercase and must not end in `.git`.
4. **Settings → Access Tokens**: create a token with `write_registry` and
   `read_registry`.
5. Log in to the registry. The username is your GitLab username, and the password is
   the token from step 4:

   ```bash
   docker login gitlab-registry.nrp-nautilus.io
   ```

Check by pushing a throwaway image:

```bash
P=gitlab-registry.nrp-nautilus.io/<user>/<project>
docker pull ubuntu:24.04
docker tag ubuntu:24.04 "$P/probe:test"
docker push "$P/probe:test"               # ends with "test: digest: sha256:..."
```

Afterwards, delete `probe` under Deploy → Container Registry. If the push fails with
`push access denied ... insufficient_scope`, either the path does not match the project
(check lowercase and the `.git` suffix) or the token lacks `write_registry`.

### Setup 5. Check the model endpoint

```bash
export OPENAI_BASE_URL=https://<endpoint>/v1
export OPENAI_API_KEY=<key>
curl -s "$OPENAI_BASE_URL/chat/completions" -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-small","messages":[{"role":"user","content":"Say OK"}],"max_tokens":200}'
```

Expect JSON with a `choices` entry. The endpoint serves a reasoning model, so with a
small `max_tokens` the whole reply can land in `reasoning` with `content: null`. That
still means it works. To list the model ids the endpoint accepts:

```bash
curl -s "$OPENAI_BASE_URL/models" -H "Authorization: Bearer $OPENAI_API_KEY"
```

### Setup 6. Set up this directory

Place it as described in "Where this directory goes", then from its root:

```bash
uv sync                                   # dependencies for generation and the tools
export HARBOR_DIR=/absolute/path/to/harbor
export UV_PYTHON=3.12 PYTHONPATH=src
export OPENAI_BASE_URL=https://<endpoint>/v1 OPENAI_API_KEY=<key>
```

Check:

```bash
python3 tests/test_label_consistency.py                   # "all four sources agree"
uv run --project "$HARBOR_DIR" python -V                  # Python 3.12.x
uv run --project "$HARBOR_DIR" python -c "import bespoke_stratos_traj_analysis.opencode_watchdog; print('ok')"
```

If the last check fails with `No module named 'bespoke_stratos_traj_analysis'`, either
`PYTHONPATH=src` is not set or you are not in this directory.

Setup is done. Continue with "Quick start", or with "Step by step" for what each
command does.

---

## Where this directory goes

Every path in the configs and scripts is relative to this directory's root, and Harbor
is found only through `HARBOR_DIR`. So the directory works in either of two places
with no code changes.

### Next to a Harbor checkout (default)

```
somewhere/
├── harbor/            # any Harbor with the `ack` environment
└── setup_bespoke/     # this directory
```

```bash
cd setup_bespoke
export HARBOR_DIR="$(cd ../harbor && pwd)"   # absolute, so it survives a later cd
```

### Inside a Harbor fork

Put the contents at `adapters/bespoke-stratos-traj-analysis/`. If an older copy is
already there, replace it completely; its configs used a different layout.

```bash
cd <fork>
rm -rf adapters/bespoke-stratos-traj-analysis
cp -R /path/to/setup_bespoke adapters/bespoke-stratos-traj-analysis
rm -rf adapters/bespoke-stratos-traj-analysis/{.venv,datasets,jobs,output}   # never commit these

cd adapters/bespoke-stratos-traj-analysis
export HARBOR_DIR="$(git rev-parse --show-toplevel)"
```

Everything else in this README applies unchanged. Run every command from
`adapters/bespoke-stratos-traj-analysis/`.

**Keep this directory's `.gitignore`.** The fork's root `.gitignore` ignores `/datasets/`
and `/jobs/` only at the repository root. It does not cover the `datasets/`, `jobs/`, and
`output/` created here, which reach several GB. Before committing, check that
`git status` lists only source files:

```bash
git status --short adapters/bespoke-stratos-traj-analysis/   # no datasets/, jobs/, output/
```

### Handing it to someone else

The other person needs:

- **A Harbor with the `ack` environment.** Upstream
  [harbor-framework/harbor](https://github.com/harbor-framework/harbor) `main` works, as
  does the `bespoke_adapter` branch of
  [Lucas-SJY/harbor](https://github.com/Lucas-SJY/harbor/tree/bespoke_adapter). That
  fork's `main` is Harbor 0.13.2 and has no `ack`, so it does not work.
- **NRP access**: their own kubeconfig, kubelogin, and a namespace they can create pods
  in. If they share yours, they use `wang-research-lab` unchanged.
- **The inference endpoint and an API key.** Give them a key through a private
  channel. Never commit it, and never put it in a config file.

They can usually **skip building the image.** If your GitLab project is public and
already has an image that contains every row (the `--limit 16710` image), they
generate tasks with `--shared-image` set to that exact reference and go straight to
running. Pods pick their own trajectory from the image by id, so any subset of rows
works with it.

What they must change before running, in the config they use:

| Setting | Why |
|---|---|
| `namespace` | If they run in their own namespace |
| `job_name` | If they share a machine or `jobs/` directory with your runs, to avoid colliding with your job directories |
| `n_concurrent_trials` | To stay within the namespace quota; two people at 32 each share one quota |

---

## Environment variables

| Variable | Required | Value | Why |
|---|---|---|---|
| `HARBOR_DIR` | yes | path to your Harbor checkout | Commands run Harbor with `uv run --project "$HARBOR_DIR"` |
| `UV_PYTHON` | **yes** | `3.12` | See below. Without it every trial fails before a pod is created |
| `PYTHONPATH` | **yes** | `src` | The configs load the agent by import path from `src/` |
| `OPENAI_BASE_URL` | yes | e.g. `https://<gateway>/v1` | Model endpoint used by the agent |
| `OPENAI_API_KEY` | yes | your key | Model endpoint key |
| `HF_TOKEN` | no | HF token | Only for higher Hugging Face rate limits during generation |
| `KUBECONFIG` | no | path | Only if your kubeconfig is not `~/.kube/config` |

```bash
export HARBOR_DIR=/path/to/harbor
export UV_PYTHON=3.12
export PYTHONPATH=src
export OPENAI_BASE_URL=https://<your-nrp-inference-endpoint>/v1
export OPENAI_API_KEY=<your-key>
```

**Why `UV_PYTHON=3.12` matters.** Harbor's `.python-version` pins 3.13, so `uv run`
would otherwise build or rebuild the venv on 3.13. Python 3.13's `ssl` enables
`VERIFY_X509_STRICT` by default. NRP's cluster CA was issued in 2018 without an
Authority Key Identifier extension, so 3.13 rejects it. Every trial then dies on the
first Kubernetes API call with `MaxRetryError ... Missing Authority Key Identifier`.
`kubectl` itself is unaffected, so the cluster looks healthy from the shell.
Check before every run:

```bash
uv run --project "$HARBOR_DIR" python -V      # must print Python 3.12.x
# if it prints 3.13.x:
(cd "$HARBOR_DIR" && rm -rf .venv && UV_PYTHON=3.12 uv sync --all-extras)
```

Environment variables do not carry over into new terminals or new tmux windows.
Set them again in each one.

---

## Quick start

```bash
cd setup_bespoke
export HARBOR_DIR=/path/to/harbor UV_PYTHON=3.12 PYTHONPATH=src
export OPENAI_BASE_URL=https://<endpoint>/v1 OPENAI_API_KEY=<key>
IMAGE=gitlab-registry.nrp-nautilus.io/<user>/<project>/bespoke-stratos:v1

# 1. generate tasks (first 6000 rows) and the shared-image build context
uv run bespoke-stratos-traj-analysis --limit 6000 --overwrite --shared-image "$IMAGE"

# 2. build and push the one image every task uses
docker login gitlab-registry.nrp-nautilus.io
./scripts/build_and_push_shared_image.sh datasets/bespoke-stratos-traj-analysis-shared-image "$IMAGE"

# 3. edit configs/nrp.yaml: namespace, registry, n_concurrent_trials

# 4. run (inside tmux; this takes days)
uv run --project "$HARBOR_DIR" harbor run --config configs/nrp.yaml

# 5. export the accepted annotations
./scripts/export.sh jobs/bespoke-stratos-v2 datasets/bespoke-stratos-traj-analysis output/labels
uv run python scripts/view_annotation.py --labels output/labels --check
```

The steps below explain each command and what to check after it.

---

## Step by step

### 1. Install the generation dependencies

```bash
uv sync
```

This creates `setup_bespoke/.venv` with `datasets`, which the generator and
`view_annotation.py` need. It is separate from Harbor's venv.

### 2. Generate the tasks

```bash
uv run bespoke-stratos-traj-analysis --limit 6000 --overwrite \
  --shared-image gitlab-registry.nrp-nautilus.io/<user>/<project>/bespoke-stratos:v1
```

| Option | Meaning |
|---|---|
| `--limit N` | Take the first N rows of the train split (16,710 in total) |
| `--shared-image REF` | Point every task at one shared image. Use this on Kubernetes |
| `--output-dir DIR` | Default: `datasets/bespoke-stratos-traj-analysis` |
| `--overwrite` | Replace existing task directories |
| `--registry`, `--image-tag` | Legacy per-task images; see "Why one shared image" |
| `--source-model` | Recorded in each task; default `DeepSeek-R1` |
| `--hf-token` | Optional Hugging Face token |

The command writes:

- `datasets/bespoke-stratos-traj-analysis/sample_NNNNNN/`: one Harbor task per row
  (`instruction.md`, `task.toml`, `environment/`, `tests/test.sh`).
- `datasets/bespoke-stratos-traj-analysis-shared-image/`: the image build context,
  a Dockerfile plus `trajectories/sample_NNNNNN.json` for every generated row.

Check it:

```bash
ls datasets/bespoke-stratos-traj-analysis | wc -l          # == N
tail -1 datasets/bespoke-stratos-traj-analysis/sample_000000/task.toml   # docker_image = "<REF>"
```

**Ids are positional.** `sample_NNNNNN` is row NNNNNN of the train split. The generator
numbers every record it reads, including ones it skips, so ids never shift. Records
with no reasoning block are skipped and keep their number.

### 3. Build and push the shared image

```bash
docker login gitlab-registry.nrp-nautilus.io        # user = GitLab username, password = access token with write_registry
./scripts/build_and_push_shared_image.sh \
  datasets/bespoke-stratos-traj-analysis-shared-image \
  gitlab-registry.nrp-nautilus.io/<user>/<project>/bespoke-stratos:v1
```

The image reference must match the `--shared-image` value from step 2 exactly. The
script builds for `linux/amd64`, since NRP nodes are x86_64, pushes the image, and then
checks that it can select a trajectory the same way a pod does. It ends with
`=== ok: ... verified ===`.

**Use a new tag whenever the image content changes.** Tags other than `latest` are
pulled `IfNotPresent`, so nodes keep using a cached image under the same tag. A pod
that gets an old image without the new rows exits with `no trajectory for '<id>'`.

**Why one shared image.** Pushing a new image uploads the ~29 MB base layer every time,
about 110 s per push against NRP GitLab. Parallel pushes do not help, because the
uplink is the bottleneck. Putting tags in one repository does not help either, because
buildx recompresses layers and the registry cannot deduplicate them. 6000 per-task
images came to ~174 GB and ~183 h of uploading. The shared image is a single push. Each
pod copies its own trajectory to `/trajectory.json` at startup, using its `environment`
label through the Kubernetes Downward API, and fails loudly if the file is missing. That
command is in `pod_overrides` in the NRP configs. `build_and_push_images.sh` and
`--registry` are kept only for small experiments.

### 4. Configure

Edit `configs/nrp.yaml` before the first run:

```yaml
# excerpt -- the real file has more under each key
n_concurrent_trials: 32          # pods at once; your namespace quota caps this
environment:
  kwargs:
    namespace: wang-research-lab
    registry: gitlab-registry.nrp-nautilus.io/<user>/<project>
agents:
  - kwargs:
      max_blocking_threads: 128  # must stay ABOVE n_concurrent_trials
```

**Do not edit a config after its run has started.** Harbor resumes a job only if the
stored `config.json` and `lock.json` match exactly. A change raises `FileExistsError`,
and the only way out is a new job directory, which reruns everything.

`max_blocking_threads` matters because Harbor's `ack` environment runs each in-pod
command through `asyncio.to_thread`, and each command holds a worker thread until it
finishes. Python's default pool is `min(32, cpu_count + 4)` threads. Without raising it,
only that many trials make progress while the rest hold idle pods, and NRP bans
namespaces that sit on idle requests.

### 5. Validation run (recommended)

Run 20 tasks before committing days of cluster time. Add `n_tasks` to the dataset
entry at the end of the config:

```yaml
datasets:
  - path: datasets/bespoke-stratos-traj-analysis
    n_tasks: 20
```

```bash
kubectl delete pods -l app=sandbox -n wang-research-lab      # clear leftovers
uv run --project "$HARBOR_DIR" harbor run --config configs/nrp.yaml
cat jobs/bespoke-stratos-v2/*/verifier/validation.txt | sed -E 's/[0-9]+/N/g' | sort | uniq -c
```

Expect mostly `OK: N span(s) tiling N segment(s)`.

**Before the full run, do both of these:** remove the `n_tasks` line, and delete the
job directory (`rm -rf jobs/bespoke-stratos-v2`). `lock.json` records the exact task
list, so a 20-task job cannot be resumed as a 6000-task one.

### 6. Full run

```bash
tmux new -s bespoke
# in tmux: re-export the environment variables, then
uv run --project "$HARBOR_DIR" harbor run --config configs/nrp.yaml
# detach: Ctrl-B then D        reattach: tmux attach -t bespoke
```

Run it inside tmux. Closing a plain terminal kills Harbor, and pods left behind keep
their quota until `activeDeadlineSeconds` (2 h) expires.

If the run is interrupted, run the same command again. Trials that already have a
`result.json` are skipped.

Rough timing from the first 6000 rows: 93 h at concurrency 32, about 64 trials/hour.

### 7. Monitor

```bash
ps aux | grep -c "[h]arbor run"                                                     # > 0
kubectl get pods -l app=sandbox -n wang-research-lab --no-headers | grep -c Running # ~ n_concurrent_trials
date -r jobs/bespoke-stratos-v2/job.log '+%H:%M:%S'                                 # a few minutes old at most
ls jobs/bespoke-stratos-v2 | grep -c sample                                         # trials started
find jobs/bespoke-stratos-v2 -path '*verifier/reward.txt' -exec grep -l '^1' {} + | wc -l   # accepted
```

If any of the first three checks is wrong, the run is dead.

### 8. Retrying failures

The verifier is strict, so `reward == 1` reliably marks a usable trial. To retry
everything else with unchanged settings, delete those trial directories and rerun the
same config:

```bash
J=jobs/bespoke-stratos-v2
n=0; for d in "$J"/sample_*; do [ "$(cat "$d/verifier/reward.txt" 2>/dev/null)" = "1" ] || n=$((n+1)); done; echo "will delete $n"
for d in "$J"/sample_*; do [ "$(cat "$d/verifier/reward.txt" 2>/dev/null)" = "1" ] || rm -rf "$d"; done
uv run --project "$HARBOR_DIR" harbor run --config configs/nrp.yaml
```

This works because `lock.json` is built from the planned task list, not from the trial
directories on disk. Missing directories are simply run again.

To retry with **different** settings, such as lower concurrency, use
`configs/nrp-retry.yaml` with a dataset that holds only the remaining tasks:

```bash
./scripts/make_retry_dataset.py --source datasets/bespoke-stratos-traj-analysis \
  --labels output/labels --dest datasets/bespoke-stratos-retry --overwrite
uv run --project "$HARBOR_DIR" harbor run --config configs/nrp-retry.yaml
```

On the first 6000 rows, retry rounds recovered 41% and then 30% of what was left: 65%
usable after the first round, 80% after the second, 86% after the third. Returns shrink
because the hardest samples are the ones left.

### 9. Export

```bash
./scripts/export.sh jobs/bespoke-stratos-v2 datasets/bespoke-stratos-traj-analysis output/labels
```

This runs two steps in order:

1. `aggregate_labels.py` expands the ranges of each **accepted** trial (`reward == 1`)
   into spans with text and word counts, plus a summary.
2. `add_question.py` attaches the original question. Step 1 does not write it, so
   skipping this step leaves the new files without the field.

Both steps skip files that already exist. You can export several jobs into the same
`output/labels`, or run the export again after more trials finish.

### 10. Verify against the original dataset

```bash
uv run python scripts/view_annotation.py --labels output/labels --stats   # coverage, no download
uv run python scripts/view_annotation.py --labels output/labels --check   # verify every file
uv run python scripts/view_annotation.py --labels output/labels --id sample_000123
```

`--check` loads the pinned dataset revision and confirms that, for every annotation,
the `question` is byte-identical to the row's user turn and the spans reassemble the
row's reasoning block. It exits non-zero on any failure. `--id` prints the raw row next
to the annotation.

---

## Second pass: the rest of the dataset

Generate every row, then keep only the rows that do not have an annotation yet. Ids
are positional, so the kept tasks cannot collide with the first pass.

```bash
IMAGE=gitlab-registry.nrp-nautilus.io/<user>/<project>/bespoke-stratos:v2   # new tag: new content
uv run bespoke-stratos-traj-analysis --limit 16710 --overwrite \
  --output-dir datasets/bespoke-stratos-all --shared-image "$IMAGE"
./scripts/build_and_push_shared_image.sh datasets/bespoke-stratos-all-shared-image "$IMAGE"

./scripts/make_retry_dataset.py --source datasets/bespoke-stratos-all \
  --labels output/labels --dest datasets/bespoke-stratos-rest --overwrite

uv run --project "$HARBOR_DIR" harbor run --config configs/nrp-rest.yaml
./scripts/export.sh jobs/bespoke-stratos-rest datasets/bespoke-stratos-rest output/labels
```

`configs/nrp-rest.yaml` uses its own `job_name`, because a job directory can only be
resumed with the same task set.

---

## Local Docker run

For small experiments, trials can run in local Docker with only the model remote.
Generate **without** `--shared-image` and `--registry`, so each task builds its own
image:

```bash
uv run bespoke-stratos-traj-analysis --limit 20 --overwrite
uv run --project "$HARBOR_DIR" harbor run --config configs/local-docker.yaml
```

This path needs neither `PYTHONPATH` nor the cluster.

---

## Output format

`output/labels/sample_NNNNNN.json`:

```json
{
  "id": "sample_000000",
  "model": "DeepSeek-R1",
  "question": "Return your final response within \\boxed{}. The operation ...",
  "spans": [
    {"start": 0, "end": 0, "label": "planning_next_step",
     "text": "Okay, let me try to figure out this problem.", "token_count": 9},
    {"start": 1, "end": 3, "label": "restating_problem", "text": "...", "token_count": 27}
  ],
  "summary": {
    "total_token_count": 673,
    "restating_problem": {"token_count": 27, "percentage": 4.01},
    "logical_deduction": {"token_count": 385, "percentage": 57.21}
  }
}
```

- `model` is the model that **wrote the reasoning trace** (the whole dataset is
  distilled from DeepSeek-R1). It is not the annotator.
- `start`/`end` are inclusive segment indices.
- `token_count` is a whitespace word count, not a tokenizer count.
- `summary` always has all eight labels, with zeros for unused ones. Because it is
  computed from the spans, the counts always add up exactly.
- Span texts are joined with single spaces, so the trace is reproduced exactly up to
  whitespace.

**Mapping to the original dataset.** Bespoke-Stratos-17k has no id column. The id is
the row number in the train split at revision `9e9adba943911a9fc44dffcb30aaa18dc96ae6df`:

```python
from datasets import load_dataset
rows = load_dataset("bespokelabs/Bespoke-Stratos-17k", split="train",
                    revision="9e9adba943911a9fc44dffcb30aaa18dc96ae6df")
row = rows[123]          # sample_000123
```

If you load a different revision, match on `question` instead, since rows may be in a
different order.

---

## Tuning the agent

These are set in the `kwargs` of the agent in each NRP config:

| Setting | Default | What it does |
|---|---|---|
| `idle_timeout_sec` | `600` | Kill OpenCode after this long with no new events, then let the verifier judge what it left |
| `heartbeat_interval_sec` | `20` | Write a byte to stderr this often so the cluster does not drop the connection; `0` disables it |
| `poll_interval_sec` | `15` | How often the watchdog checks for new events |
| `max_blocking_threads` | `128` | Size of Harbor's worker-thread pool; keep it above `n_concurrent_trials` |

The agent-level cap is the task's `timeout_sec` (1800) times
`agent_timeout_multiplier` (1.33), about 40 minutes.

**On `idle_timeout_sec`.** Single model calls can be slow. In the first pass, the
median of each trial's longest gap between events was 282 s, and the 90th percentile
was 521 s. In an October validation run, 11 of 20 trials were killed at the 10-minute
watchdog limit while still waiting for the model. If many trials end
`labels.json does not exist` with no exception and their last event about 10 minutes
before the end, raise `idle_timeout_sec`, for example to 1200. That is a config change,
so start a new job directory.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Every trial: `MaxRetryError ... Missing Authority Key Identifier` | Harbor's venv is Python 3.13 | `export UV_PYTHON=3.12`, rebuild Harbor's venv (see "Environment variables") |
| `No module named 'bespoke_stratos_traj_analysis'` | `PYTHONPATH=src` not set, or not running from `setup_bespoke/` | Set it and `cd` here |
| `Connection to remote host was lost.` about 60 s after the last event | NRP drops exec connections idle for 60 s | The heartbeat prevents this. Check `heartbeat_interval_sec` is > 0 and well under 60 |
| `labels.json does not exist`, no exception, last event ~10 min before end | Watchdog `idle_timeout_sec` fired while the model was still thinking | Raise `idle_timeout_sec` (new job directory) |
| 403 from Gatekeeper: `container <builder> does not have <{"cpu","memory"}> requests` | Harbor tried to build the image in the cluster | Keep `skip_image_check: true` and `force_build: false`, and push the image yourself |
| Pod exits immediately, log says `no trajectory for '<id>'` | Node uses a cached older image without these rows | Push under a **new** tag and regenerate the tasks with it |
| Verifier: `No module named 'json'` | Image built from an old Dockerfile using `python3-minimal` | Rebuild with the current template under a new tag |
| `ImagePullBackOff` | Image not pushed, project not public, or registry returning 502/503 | Check `docker manifest inspect <ref>`; 502/503 are transient |
| `FileExistsError: ... lock.json that does not match` | Config or task set changed since the job started | Undo the change, or delete the job directory |
| Config rejected, `ack` unknown | Harbor too old | Use a Harbor with the `ack` environment (tested: 0.20.0) |
| `zsh: no matches found: ...` | A glob matched nothing, so zsh aborted the whole command | The path is wrong or the directory is empty; nothing ran |

To confirm the watchdog/heartbeat wrapper is actually applied, look for
`hb_opencode_run.sh` in a trial's `trial.log`. The wrapper subclasses Harbor's
`OpenCode` agent and recognises the run command by the substring
`" run --format=json "`. If a different Harbor version builds that command differently,
the wrapper silently stops applying.

---

## Notes

**The annotator behind the model alias can change.** The configs request
`nrp/qwen3-small`. On 2026-10-02 the gateway answered as `Qwen/Qwen3.8-27B`, and
OpenCode does not record the served model anywhere. If you mix annotations from runs
far apart in time, they may come from different models. Keep each run's output
separate, or record which run produced each file.

**NRP cluster policy.** Each pod requests exactly 1 CPU / 2 Gi, which is the documented
exemption from NRP's utilization thresholds. Every limit equals its request. No GPUs are
used. Pods are not privileged. `activeDeadlineSeconds` caps each pod at 2 h, well under
the 6 h limit for pods without a controller. Raise `n_concurrent_trials` only within
your namespace quota. NRP judges namespaces by the requests they hold, not the
resources they use.

**Checking the label set.** The eight label names appear in the instruction, the
verifier, the aggregator, and the adapter. After changing any of them, run:

```bash
python3 tests/test_label_consistency.py
```
