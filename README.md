# EvidenceEvolve

Product direction: [EvidenceEvolve as an AI Research Operating System](docs/AI_RESEARCH_OS.md).

EvidenceEvolve is an evidence-gated, reproducible algorithm discovery harness. It separates creative search from scientific authority:

```text
candidate proposal -> isolated implementation -> deterministic evaluation
                   -> evidence/closure/safety gates -> immutable receipt
```

GPT/Codex can propose, implement, and explain candidates. It cannot edit campaign rules or decide its own fitness. Scientific decisions are derived from frozen contracts, raw metrics, controls, and deterministic rules.

## R0 scope

Implemented in this repository:

- research-contract validation and content locking;
- claim-aware candidate genomes;
- A/B/C/D evidence permissions;
- closure/reopen enforcement;
- hard-constraint and four-outcome gate semantics;
- idempotent budget accounting;
- create-once, stage-addressed JSON receipts;
- SQLite receipt history and lineage index without candidate-stage overwrite;
- bound verdict replay and frozen-evaluator re-execution;
- Git worktree scope auditing;
- Codex CLI command construction with role-specific sandboxes;
- a commit-pinned `SHINKA_NATIVE` engine that runs the official ShinkaEvolve
  runner and imports its database without changing search behavior;
- a synthetic trap canary for protocol tampering, missing evidence, safety regression, and a valid positive.
- a bounded ONNX graph-rewrite engineering canary adapter.

Not claimed by R0: an autonomous campaign loop, live Shinka evolution, physically blind confirmation, QNN/device deployment, algorithm novelty, or safety evidence. The ONNX task is an engineering harness canary, not scientific or product authority.

## R1 core slice

The repository now also contains the first executable slice of **EvidenceEvolve R1 — Evidence-Guided Open-Ended Discovery**:

- candidate genomes can declare the search abstraction, mechanism claims, assumptions, behavior descriptors, ablations, transfer motifs, failure risks, and estimated information value;
- a research-policy genome ranks candidates by admission likelihood, expected improvement, information gain, novelty, transfer value, cost, and redundancy;
- closure is a non-bypassable scheduling boundary: a closed family is ineligible even when its acquisition score would otherwise dominate;
- `CampaignRunner` runs one bounded generation through a contract-frozen task adapter, re-audits changed files and closures, derives the verdict with `GateEngine`, writes an immutable receipt, and resumes idempotently;
- the understanding loop compares preregistered expected signatures with frozen metrics and reference metrics, then combines controls and ablations into `INTERVENTION_SUPPORTED`, `PREDICTION_SUPPORTED`, `CONTRADICTED`, `INCONCLUSIVE`, or `NOT_EVALUABLE`;
- mechanism assessments are explicitly `SCHEDULING_ONLY`; they cannot change the four scientific outcomes;
- policy candidates can be compared on blind held-out meta-benchmark observations, but passing only yields `ELIGIBLE_FOR_HUMAN_PROMOTION`.

This is a working orchestration and causal-credit core, not a claim that open-ended discovery has already succeeded. Beyond the bounded Codex slice below, a real ChronoDiscovery task suite, literature reproduction/mechanism cards, policy mutation campaigns, upstream live-result parity, and any blind evidence that R1 discovers algorithms more efficiently than the baselines are still missing.

## R1 autonomous loop slice

The bounded Codex loop and its persistent R1.2 population layer are implemented:

```text
Codex proposal (read-only, schema-bound)
  -> policy and closure admission
  -> candidate-specific Git worktree
  -> Codex implementation (workspace-write)
  -> frozen task observation adapter
  -> CampaignRunner / GateEngine verdict and immutable receipt
  -> archived verdict and mechanism feedback in the next proposal
```

Candidate IDs are fixed by generation and proposal slot before Codex runs, so
proposal and implementation budget reservations remain idempotent across resume.
Each candidate now names one `genetic_parent_id`; the candidate worktree starts at
that evaluated parent's commit rather than restarting from the authority baseline.
Receipts preserve both the immutable comparison base and the actual genetic parent,
plus baseline-relative and parent-relative patch hashes. Valid negatives can remain
failure-directed search seeds, while invalid artifacts are quarantined; neither
search role changes the frozen scientific outcome or claim ceiling.

The default policy also executes its mutation mix, records a per-generation policy
effect trace, reserves a small moonshot fraction, and enters structural
`BREAKTHROUGH` search after bounded stagnation. One candidate-local proposal or
implementation failure no longer aborts the rest of the generation. Historical
receipts are compiled into source-bound Result, Failure, Mechanism, Lineage,
Frontier, and Procedure cards. Role-scoped FTS5/structured retrieval feeds the
next proposal without exposing confirmation receipts or granting memory to the
Gate Engine. A scheduling-only Research Director uses those cards to select a
research action and can reallocate the next generation toward controls, ablations,
failure-directed tests, transfer, or breakthrough mutations.

The first independent Research Action executor is also available. A
`SEARCH_LITERATURE` job can search OpenAlex works, inspect public GitHub
repositories, pin repository evidence to commit/tree/blob snapshots, write an
immutable scheduling-only action receipt, and compile compact external-source
cards. When live intelligence is enabled in the autonomous loop, the Director
can retrieve those new cards and make a second decision in the same generation.
External sources remain inspiration, never scientific or promotion authority.

R1.2 persists code artifacts and island memberships in the campaign database. Each
slot is assigned to an island; parents are sampled across elite, novelty,
failure-directed, explicit stepping-stone, and migrant roles; and bounded ring
migration shares artifacts between islands. Active populations are capacity-limited
without deleting history. Exact baseline-relative code duplicates are rejected
before the frozen evaluator is called. Proposal and evaluation worker counts are
finite policy effects recorded in the per-generation trace, with deterministic
result ordering.

Codex cannot provide reference metrics, verified reopen evidence, a confirmation
stage, or a verdict. The implementer is limited to the candidate worktree; actual
changed paths are independently audited before the frozen evaluator result reaches
the gate.

The design boundary, implemented R1.1/R1.2 semantics, and ordered next slices are in
[`docs/DISCOVERY_ARCHITECTURE.md`](docs/DISCOVERY_ARCHITECTURE.md).

An execution-only SSH CPU worker can run commit- and input-bound tests or evaluators
without moving the controller, archive, or GateEngine off the local machine. The
worker returns a tamper-evident operational receipt with no scientific authority;
setup and usage are documented in
[`docs/REMOTE_CPU_WORKER.md`](docs/REMOTE_CPU_WORKER.md).

Run the five-island, five-generation ONNX mechanics campaign with a Codex CLI login
(ChatGPT-managed login is supported; an API key is not required):

```powershell
evolve campaign autonomous research/contracts/onnx_rewrite_r1.locked.yaml `
  --policy research/policies/r1_2_islands_a0.yaml `
  --adapter tasks.onnx_rewrite.autonomous_adapter:evaluate_candidate `
  --run-dir runs/onnx_rewrite_r1_2_a0 `
  --generations 5 `
  --proposals-per-generation 5
```

Add `--enable-live-intelligence` to let a `SEARCH_LITERATURE` Director decision
execute before candidate proposal. The per-action paper/repository/file limits
come from the frozen research-policy genome.

Use `--codex-executable PATH` when `codex` does not resolve to a usable standalone
CLI. The command fails closed before proposal when the executable cannot start.
The loop is resumable by rerunning the same command: validated proposals,
implementations, receipts, and budget reservations are reused rather than counted
again.

This slice establishes executable search mechanics and source acquisition, not
autonomous-discovery evidence. Independent `REPLICATE` and `ACQUIRE_EVIDENCE`
executors, semantic closure, cross-campaign transfer validation, strategy
meta-learning, Red Queen populations, blind confirmation, algorithm novelty, and
superiority over a baseline remain unproven or unimplemented until the named code
and external campaigns exist.

## Three-arm benchmark protocol smoke

The first comparative benchmark vertical slice is now executable. It freezes a
CPU-only graph-coloring task, ten paired seeds, equal per-trial budget ceilings,
development-only candidate selection, and post-selection public-fresh evaluation
for `VANILLA_CODEX`, `EVIDENCE_EVOLVE_NO_MEMORY`, and
`EVIDENCE_EVOLVE_FULL`.

```powershell
evolve benchmark validate-protocol `
  benchmarks/graph_coloring/three_arm_v0.locked.yaml

evolve benchmark run-graph-coloring `
  benchmarks/graph_coloring/three_arm_v0.locked.yaml `
  --run-dir runs/graph_coloring_three_arm_v0_smoke
```

The built-in adapter submits the same frozen solver for every arm, so this command
tests pairing, budget enforcement, immutable receipts, resumption, and evaluator
symmetry only. The public-fresh instances are visible in the repository and are
not blind confirmation. Results are hard-coded by schema to remain
`NOT_EVALUABLE_BLIND_CONFIRMATION_UNAVAILABLE`, with no superiority authority.
The frozen design and real-arm adapter boundary are documented in
[`docs/BENCHMARK_PROTOCOL.md`](docs/BENCHMARK_PROTOCOL.md).

A separate one-seed live integration protocol now binds the actual execution
paths: governed Vanilla Codex iteration, EvidenceEvolve with deterministic empty
memory packets, and full EvidenceEvolve receipt-bound memory. Each arm receives
two proposal and implementation opportunities. This remains an integration pilot,
not a statistical comparison or superiority claim:

```powershell
evolve benchmark run-graph-coloring `
  benchmarks/graph_coloring/three_arm_live_pilot_v0.locked.yaml `
  --run-dir runs/graph_coloring_three_arm_live_pilot_v0
```

## Research intelligence action

OpenAlex currently requires an API key for API use. GitHub public repository
inspection can run without a token, although a token is recommended for a larger
rate limit. Secrets are read from environment variables and are not stored in
jobs, receipts, source URLs, or memory cards.

```powershell
$env:OPENALEX_API_KEY = "..."
$env:GITHUB_TOKEN = "..." # optional for public repositories

evolve research-action search-literature runs/onnx_rewrite_r1_2_a0 `
  --query "mobile monocular depth false-block support estimation"
```

Use `--max-papers 0` for a repository-only action when no OpenAlex key is
available. A missing required credential returns `WAITING_FOR_AUTHORITY` before
budget is consumed. Rerunning the same action ID returns the existing bound
receipt rather than repeating external calls.

## Setup

Python 3.11 is recommended for repository development. The pinned ShinkaEvolve
upstream supports Python 3.10+, while the EvidenceEvolve package supports Python
3.11-3.14.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\evolve.exe --help
```

## First run

The repository ships an unlocked draft contract. Lock it against a committed baseline before using it as authority:

```powershell
evolve lock-contract research/contracts/synthetic_canary_r0.draft.yaml `
  --output research/contracts/synthetic_canary_r0.locked.yaml

evolve validate-contract research/contracts/synthetic_canary_r0.locked.yaml
evolve run-canary research/contracts/synthetic_canary_r0.locked.yaml
evolve inspect runs/synthetic_canary_r0
evolve replay runs/synthetic_canary_r0
evolve replay-evaluation runs/synthetic_canary_r0
```

`lock-contract` resolves `HEAD`, hashes every frozen asset, and seals the canonical contract payload. `validate-contract` then rejects drift, an incomplete authority TCB, mutable evaluators/adapters, visible confirmation policy, invalid evidence permissions, missing hard gates, and editable-scope conflicts.

`replay` validates receipt-to-contract bindings and recomputes deterministic gate verdicts. `replay-evaluation` additionally re-runs the campaign's frozen evaluator adapter. Runtime latency is reported as observational replay drift rather than treated as byte-deterministic evidence.

## Codex and upstream-first ShinkaEvolve

The Codex backend emits `codex exec --json --output-schema ...` commands and grants `workspace-write` only to the implementer role. Read-only roles use the default read-only sandbox. Authentication is deliberately outside contract files and receipts.

`SHINKA_NATIVE` now delegates configuration and execution to the commit-pinned
official ShinkaEvolve 0.0.7 API. EvidenceEvolve does not replace its parent
sampling, islands, novelty rejection, model bandit, prompt evolution, database,
checkpoint/resume behavior, or WebUI artifacts. It adds a source-bound import
receipt after the native run:

```powershell
evolve search shinka-native `
  --run-id circle-parity-seed-001 `
  --task-dir PATH_TO_SHINKA_TASK `
  --config-fname shinka_small.yaml `
  --results-dir runs/shinka-native/circle-parity-seed-001 `
  --num-generations 20
```

The native `combined_score` remains `SHINKA_SCHEDULING_ONLY`. The import receipt
has no scientific-outcome authority and cannot support a superiority claim.
Deterministic construction parity and the single-worker actual-runner P0 surface
are tested against the pinned upstream CLI. Upstream's unseeded SQLite random
archive-inspiration branch remains outside exact sequence parity. The P1
real-provider integration smoke also ran on the upstream circle-packing task
through subscription-backed Codex CLI (`gpt-5.5`, high effort): both entrypoints
made five proposal LLM calls and resumed native state, while one native-arm
proposal failed official patch application and left that arm at 5/6 completed
generation slots. P1 therefore passes as an integration smoke only; its single
unseeded trajectories do not prove score/token/cost parity. Multi-seed
circle-packing non-inferiority remains an unmet promotion gate. See
[`docs/UPSTREAM_SEARCH_KERNELS.md`](docs/UPSTREAM_SEARCH_KERNELS.md).

P2-R0 is preserved as `CLOSED_NOT_EVALUABLE`: all 68 real proposals failed in
the shared upstream patch executor before any proposal candidate reached the
evaluator. P2-M0 traced this to SEARCH blocks omitting blank lines and admitted
a conservative shared materializer on 20 frozen replays (20/20 patchable,
20/20 compilable, 20/20 evaluator reached; no model calls). Both the official
CLI wrapper and `SHINKA_NATIVE` can use that same adapter without modifying
upstream files. This mechanics PASS has no search-quality authority, and P2-R1
has not run. P2-R1 is now execution-complete frozen as a fresh, unstarted lineage with symmetric
50-call arm budgets, full candidate-survival denominators, no post-hoc
replacement, carry-forward handling for invalid/missing slots, a paired -1%
primary non-inferiority gate over ten block-pair inference units, isolated
arm/block state, request-level prompt/payload receipts, and separate
invalid/useful-rate hard guardrails.
Future remote generation must not use `gpt-5.5`; the frozen R1 model is
`gpt-5.6-terra`.

The formal schedule is generated only from the frozen protocol. Formal budget
is locked until the same production path first completes a 100-slot local
transport E2E through the frozen analyzer, then a separate one-slot-per-arm real
provider smoke. Run these with `--mode ZERO_CALL_E2E` and
`--mode REMOTE_SMOKE`; `--mode FORMAL` requires both receipt paths. Smoke data
has no scientific authority and cannot be reused as a formal slot.

## R1 generation interface

Export the JSON Schemas first; `campaign_candidate.schema.json` describes every pool entry and `research_policy.schema.json` describes the mutable policy genome:

```powershell
evolve export-schemas --output-dir schemas
```

Then run or resume one generation through a task adapter whose source file is hashed as `kind: adapter` in the locked contract:

```powershell
evolve campaign run CONTRACT POOL `
  --policy research/policies/r1_default.yaml `
  --adapter package.frozen_adapter:evaluate_candidate `
  --run-dir runs/CAMPAIGN

evolve campaign resume CONTRACT POOL `
  --policy research/policies/r1_default.yaml `
  --adapter package.frozen_adapter:evaluate_candidate `
  --run-dir runs/CAMPAIGN
```

The adapter receives a validated `CampaignCandidate` and must return an `EvaluationRun`. It may orchestrate candidate-specific worktrees, but it does not return a verdict. The runner independently audits scope/closure and invokes the frozen gate.

Meta-benchmark results can be compared without auto-promoting the policy:

```powershell
evolve policy evaluate-promotion BASELINE_RESULT CANDIDATE_RESULT `
  --protocol META_PROTOCOL
```
