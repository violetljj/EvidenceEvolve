# EvidenceEvolve

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
- an optional ShinkaEvolve integration seam;
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

This is a working orchestration and causal-credit core, not a claim that open-ended discovery has already succeeded. Beyond the bounded Codex slice below, live Shinka evolution, a real ChronoDiscovery task suite, literature reproduction/mechanism cards, policy mutation campaigns, and any blind evidence that R1 discovers algorithms more efficiently than the baselines are still missing.

## R1 autonomous loop slice

The first bounded Codex loop is now implemented:

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
Codex cannot provide reference metrics, verified reopen evidence, a confirmation
stage, or a verdict. The implementer is limited to the candidate worktree; actual
changed paths are independently audited before the frozen evaluator result reaches
the gate.

Run two ONNX mechanics generations with a Codex CLI login (ChatGPT-managed login is
supported; an API key is not required):

```powershell
evolve campaign autonomous research/contracts/onnx_rewrite_r1.locked.yaml `
  --policy research/policies/r1_default.yaml `
  --adapter tasks.onnx_rewrite.autonomous_adapter:evaluate_candidate `
  --run-dir runs/onnx_rewrite_r1 `
  --generations 2
```

Use `--codex-executable PATH` when `codex` does not resolve to a usable standalone
CLI. The command fails closed before proposal when the executable cannot start.
The loop is resumable by rerunning the same command: validated proposals,
implementations, receipts, and budget reservations are reused rather than counted
again.

This slice establishes executable mechanics, not autonomous-discovery evidence.
No real Codex campaign, blind confirmation, algorithm novelty, or superiority over
a baseline is claimed until those external runs occur.

## Setup

Python 3.11 is recommended because it matches ShinkaEvolve's documented development setup. The governance core also supports Python 3.12-3.14.

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

## Codex and ShinkaEvolve

The Codex backend emits `codex exec --json --output-schema ...` commands and grants `workspace-write` only to the implementer role. Read-only roles use the default read-only sandbox. Authentication is deliberately outside contract files and receipts.

ShinkaEvolve remains an optional search kernel. Its score may schedule proposals, but EvidenceEvolve verdicts never read `combined_score`; they read the frozen multi-metric gate input.

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
