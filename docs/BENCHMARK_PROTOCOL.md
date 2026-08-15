# Comparative benchmark protocol

The first benchmark vertical slice is a CPU-only graph-coloring protocol for
checking the fairness and integrity of a paired three-arm comparison:

```text
VANILLA_CODEX
EVIDENCE_EVOLVE_NO_MEMORY
EVIDENCE_EVOLVE_FULL
```

This is currently a **protocol smoke**, not evidence that any arm is better. The
development and public-fresh graph specifications are committed to the repository.
They are useful for detecting invalid candidates and orchestration bias, but they
are not blind confirmation data. Every suite result therefore remains
`NOT_EVALUABLE_BLIND_CONFIRMATION_UNAVAILABLE` and sets
`superiority_claim_permitted=false`.

## Frozen design

[`three_arm_v0.locked.yaml`](../benchmarks/graph_coloring/three_arm_v0.locked.yaml)
binds the evaluator, baseline, runner, models, and arm adapter by SHA-256. It also
freezes:

- exactly three comparator arms;
- ten paired trial seeds;
- equal per-trial proposal, candidate, token, and wall-time ceilings;
- development-only candidate selection;
- post-selection public-fresh evaluation;
- completion of every arm and seed, with no early superiority stop;
- `valid_public_fresh_improvement_per_cost` as the smoke primary metric.

Protocol validation always warns that public-fresh is not blind confirmation.
Changing a bound asset or protocol field invalidates the lock until an intentional
re-lock.

## Run the mechanics smoke

```powershell
evolve benchmark validate-protocol `
  benchmarks/graph_coloring/three_arm_v0.locked.yaml

evolve benchmark run-graph-coloring `
  benchmarks/graph_coloring/three_arm_v0.locked.yaml `
  --run-dir runs/graph_coloring_three_arm_v0_smoke
```

The built-in `scripted_protocol_smoke` adapter deliberately submits the identical
frozen greedy solver for every arm. The expected paired deltas are exactly zero.
A non-zero delta means the comparison harness is biased or non-deterministic; it
does not mean one research system discovered an improvement.

Trial requests, submissions, evaluations, and hash-bound receipts are stored under
`runs/.../trials/<ARM>/<SEED>/`. Rerunning the same directory verifies and reuses
the immutable receipts.

## Live-arm adapter boundary

A real study must provide a protocol-bound callable using `module:function`
syntax. It receives a `BenchmarkTrialContext`, which includes only the development
instances, arm identity, paired seed, equal budget ceiling, repository root, and
trial directory. It returns an `ArmTrialSubmission` with candidate solver paths and
actual proposal/token use. The runner rejects over-budget submissions and selects
candidates only on development results.

The three real adapters still need to bind these arm identities to the actual
vanilla Codex loop, a memory-disabled EvidenceEvolve loop, and the full
EvidenceEvolve loop. That future execution protocol must add externally held blind
confirmation before it can support a superiority claim. The public repository
cannot certify its own hidden confirmation set.
