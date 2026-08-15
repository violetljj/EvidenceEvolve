# Upstream search kernels and parity gates

## Principle

EvidenceEvolve is a research operating system, not a reason to reimplement a
weaker version of a mature search engine. The default admission rule is:

> **Upstream Native Invariant.** For a mature, license-compatible upstream
> research/search kernel, EvidenceEvolve must preserve a native execution path.
> A replacement or enhancement cannot become the default until an external,
> fixed-budget, multi-seed non-inferiority test passes.

The intended modes are:

| Mode | Search authority | EvidenceEvolve role | Current status |
| --- | --- | --- | --- |
| `SHINKA_NATIVE` | Official ShinkaEvolve | launch and import only | implemented |
| `SHINKA_EVIDENCE` | Official ShinkaEvolve | governance and memory outside search | not implemented |
| `OPENEVOLVE_NATIVE` | Official OpenEvolve | launch and import only | not implemented |
| `EVIDENCE_NATIVE_EXPERIMENTAL` | EvidenceEvolve | experimental/ablation kernel | existing bounded research loop |

## `SHINKA_NATIVE` boundary

The optional dependency is pinned to ShinkaEvolve source commit
`c4568adde253cacf185be3a8412c3c2142761ebe` (package version `0.0.7`). Runtime
startup rejects a different or unverifiable source commit.

`ShinkaNativeEngine` follows the pinned upstream `shinka_run` construction path:

1. upstream task validation and language inference;
2. upstream YAML and `--set` parsing;
3. upstream `EvolutionConfig`, `DatabaseConfig`, and `LocalJobConfig` defaults;
4. upstream precedence for config, overrides, results directory, and generation count;
5. direct execution by official `ShinkaEvolveRunner`.

No Shinka search score is sent to `GateEngine`. After completion, the adapter
reads `programs.sqlite` in read-only mode and writes a separate EvidenceEvolve
receipt. Shinka's database, logs, prompt database, pricing snapshot, resume state,
and WebUI compatibility remain intact.

Token counts in the first importer are explicitly best-effort because Shinka
stores successful-call usage in program metadata while failed-attempt accounting
can differ by provider. Cost ceilings remain enforced by upstream Shinka in native
mode. Formal token-parity work must close that accounting gap before a live
non-inferiority claim.

## Running the native engine

Install the pinned dependency into the repository environment:

```powershell
uv pip install --python .venv\Scripts\python.exe ".[shinka]"
```

Run a task directory containing `evaluate.py` and `initial.<ext>`:

```powershell
$env:SHINKA_PRICING_MODE = "offline" # optional reproducible catalog mode

.venv\Scripts\python.exe -m evidence_evolve search shinka-native `
  --run-id example-seed-001 `
  --task-dir PATH_TO_TASK `
  --config-fname shinka.yaml `
  --results-dir runs/shinka-native/example-seed-001 `
  --num-generations 20 `
  --set db.num_islands=2
```

The receipt is written under
`RESULTS_DIR/evidence_evolve/receipts/RUN_ID.json`. Use a new `run-id` for a
later resume/import stage; receipts are create-once.

## Upstream parity gate

Five separate checks are required:

1. **Deterministic construction parity.** With a capturing runner, direct upstream
   CLI construction and `SHINKA_NATIVE` must receive identical effective config
   and task source. The imported receipt must preserve the runner-produced
   candidate, lineage, metric, cost, and token records. This check is covered by
   `tests/test_shinka_native.py`.
2. **Deterministic runner parity.** The actual pinned runner, driven by the same
   deterministic provider, must produce identical normalized parent, top-k
   inspiration, candidate, evaluator, archive, island, migration, stop, and
   resume events through the direct CLI and `SHINKA_NATIVE` paths. The
   single-worker deterministic surface passes in
   `tests/test_shinka_native_semantic_parity.py`. UUIDs, timestamps, result paths,
   and presentation output are normalized. Random archive-inspiration sequence
   parity remains `NOT_EVALUABLE_SEED_GAP`: upstream uses SQLite
   `ORDER BY RANDOM()`, which is not controlled by its Python/NumPy seed state.
3. **Real-provider smoke.** Run one paired official/native example with the same
   provider, model, temperature, initial program, evaluator, configuration,
   budget, concurrency, and machine. Compare candidate/valid counts, best score,
   evaluator and LLM calls, tokens, cost, wall time, and resume. P1 ran the
   pinned upstream circle-packing example through subscription-backed Codex CLI
   (`gpt-5.5`, high effort), with one proposal/evaluator worker and no dollar
   ceiling. Both arms made five proposal LLM calls and exercised resume through
   the official runner. Direct upstream completed 6/6 generation slots;
   `SHINKA_NATIVE` completed 5/6 because generation 2 produced a native
   `patch_apply_failed` attempt that the runner preserved and did not
   oversample. The wrapper raised no distinct exception, retained the native
   database/checkpoint, and wrote its non-authoritative receipts after both
   phases. This passes the real-provider integration smoke, not score, token,
   cost, or statistical parity. Failed-attempt tokens were absent from the
   upstream attempt log, and one unseeded stochastic pair cannot establish
   non-inferiority. The bounded record is
   `research/parity/shinka_native_p1_r0.result.json`.
4. **Real non-inferiority.** On the official circle-packing task with paired
   seeds, identical model/config/evaluator/budget/hardware, the median normalized
   best score must not fall more than 1% below direct Shinka. Best-so-far AUC,
   first valid improvement, cost, invalid rate, rejection rate, throughput, and
   resume consistency must also be reported.
5. **Evidence enhancement.** `SHINKA_EVIDENCE` must be non-inferior to
   `SHINKA_NATIVE` on at least three tasks and five seeds per task before it can
   become the default.

Construction parity, the deterministic P0 runner surface, and the P1
real-provider integration smoke are satisfied. The unseeded random-archive
branch and circle-packing multi-seed non-inferiority are not. No promotion gate
has passed. The engine integration is not evidence that ALE-Bench, algorithm
discovery, or scientific superiority passed. The bounded P0 and P1 records are
`research/parity/shinka_native_parity_r0.result.json` and
`research/parity/shinka_native_p1_r0.result.json`.

P2-R0 is separately frozen as `CLOSED_NOT_EVALUABLE`, not as a native-arm
failure: 68/68 proposals failed shared patch application and 0 reached the
evaluator. P2-M0 replays the frozen earliest proposal from each arm/block with
no model calls. Its shared Proposal IR/materializer admits only exact unique
line matches or unique blank-line-only normalization inside EVOLVE regions; it
rejects ambiguity, content/indentation drift, immutable edits, marker changes,
and target hash drift. The 20-case gate passed 20/20 patch, compile, and
evaluator-reached checks. Seven evaluator-invalid geometry candidates remain
candidate failures, not mechanics failures. This permits separately freezing a
P2-R1 protocol, but does not establish non-inferiority or start that campaign.
The records are `research/parity/shinka_native_p2_r0.result.json` and
`research/parity/shinka_native_p2_m0.result.json`.

P2-R1 is now execution-complete frozen as a fresh lineage and remains unstarted. Its
machine-validated record is
`research/parity/shinka_native_p2_r1.protocol.json`; freezing the protocol made
zero remote model calls. It binds `P2-R0 -> CLOSED_NOT_EVALUABLE /
NOT_EVALUABLE_DATA` and `P2-M0 -> MECHANICS_PASS / no scientific authority`
without reopening or reinterpreting either record.

The two arms each receive ten matched runs and five scheduled proposal slots
per run (50 model invocations, the same output-token ceiling, and the same
wall-time ceiling per arm). Blocks use paired local seeds and a frozen AB/BA
order. The subscription transport still exposes no model-generation seed; that
gap is recorded symmetrically and is not represented as exact model-rollout
pairing. Each arm/block has a disjoint results directory, database, state
namespace, and fresh frozen incumbent; AB/BA order has no state authority.
Failed, missing, or evaluator-invalid slots consume their declared budget and
cannot be replaced.

Every run reports the full scheduled -> model-started -> response -> extraction
-> materialization -> compile -> evaluator-reached -> evaluator-valid -> useful
funnel. The primary score trajectory starts at the frozen baseline and carries
the previous best through every invalid or missing slot. The primary estimand is
the median matched-block normalized final-best-score delta. The ten matched
block pairs are the only inference units; the 100 state-dependent proposal
slots are not treated as independent. The gate uses a -1% margin and a frozen
one-sided 95% paired-bootstrap lower-bound rule. Two hard
guardrails cannot be rescued by that score: Native evaluator-invalid rate may
be at most 10 absolute percentage points above Official, and Native useful
candidate rate per scheduled slot may be at most 10 points below Official. Ten
points equals five of the 50 predeclared slots per arm; larger attrition is
treated as operationally material. Valid-only score distributions remain
mandatory descriptive output and are never the primary denominator.

The hash closure binds the task, evaluator, config, Proposal IR/materializer,
both local execution paths, statistical analyzer, freeze-review tests, canary
definition, gate engine, and the installed upstream prompt, LLM, scheduler, and
official CLI modules. Each started slot must record the rendered system/user
prompt hashes and the payload hash for every transport attempt. Upstream may
retry the identical transport request up to three attempts with a one-second
delay and 1200-second per-attempt timeout; those attempts remain one scientific
slot. Changed-payload retry, post-hoc replacement, and scientific resampling are
forbidden. Provider-managed hidden instructions are not visible and therefore
remain explicitly outside the reproducible freeze boundary.

The execution-only admission revision adds the formal `evolve search p2-r1`
driver. It reconstructs all 20 arm-runs and 100 scientific slots from
`protocol.design.schedule`; no second handwritten schedule exists. It writes an
immutable start manifest before any provider invocation, claims disjoint
arm/block state namespaces, verifies the frozen baseline at the first-call
boundary, persists every response for deterministic crash recovery, forbids a
fourth transport attempt, and automatically constructs `P2R1AnalysisInput` for
the frozen analyzer. Completed database-receipted slots are never rerun. A
pending successful response is replayed locally after a crash; an unresolved
attempt consumes one of the three identical-payload transport attempts.
Admission also selects an actually available Node.js 18+ runtime and verifies
the frozen headless command, Codex CLI version, and OAuth status before any
proposal call; the selected executable and check-output hash enter the start
manifest.

Clean-checkout admission runs the real driver, both search engines, state,
prompt/payload audit, receipts, collector, and frozen analyzer with a local
deterministic transport and zero remote generation:

```text
evolve search p2-r1 --mode ZERO_CALL_E2E --run-root runs/p2_r1_zero_call
evolve search p2-r1 --mode REMOTE_SMOKE --run-root runs/p2_r1_smoke
```

The second command is hard-limited to one isolated slot per arm. Formal mode
will not create a remote-capable start manifest unless both PASS receipts from
the same protocol hash and executor commit are supplied.

The lineage is `2887dd19... / EXECUTION_DRIVER_NOT_FROZEN` to
`P2_R1_EXECUTION_COMPLETE_FROZEN_COMMITTED_NOT_STARTED`. The revision made zero
remote proposal calls and changed no scientific parameter, hypothesis, metric,
margin, allocation, seed policy, model policy, or decision rule.

The frozen generation model is `gpt-5.6-terra` at high effort and temperature
zero. `gpt-5.5` is explicitly forbidden. A supported R1 result has a
`PARITY_ONLY` claim ceiling: it does not imply `POSITIVE_HEADROOM`, scientific
superiority, product authority, safety authority, or deployment readiness.
