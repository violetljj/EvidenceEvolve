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

For this protocol, the denominator named `cost` is exactly one frozen candidate
evaluation. Actual proposal calls, Codex tokens, and end-to-end wall time are
recorded separately; the pilot does not collapse them into an arbitrary scalar.
Public-fresh specifications remain visible in the repository even though they are
excluded from the trial request payload.

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

A protocol-bound callable uses `module:function` syntax. It receives a
`BenchmarkTrialContext`, which includes only the development instances, arm
identity, paired seed, equal budget ceiling, repository root, and trial directory.
It returns an `ArmTrialSubmission` with candidate solver paths and actual
proposal/token use. The runner rejects over-budget submissions and selects
candidates only on development results. The CLI rejects an adapter override that
does not exactly match the adapter frozen in the protocol.

The live integration pilot is frozen separately in
[`three_arm_live_pilot_v0.locked.yaml`](../benchmarks/graph_coloring/three_arm_live_pilot_v0.locked.yaml).
It runs one paired seed with two candidate attempts per arm:

- `VANILLA_CODEX` uses the same schema-bound explorer/implementer calls, isolated
  worktrees, scope audit, and frozen development evaluator, but receives only its
  chronological iteration history. It does not use PopulationStore, Research
  Director, or scientific-memory cards.
- `EVIDENCE_EVOLVE_NO_MEMORY` runs the real autonomous population path with
  deterministic empty memory packets and records `scientific_memory_enabled=false`.
- `EVIDENCE_EVOLVE_FULL` runs the same policy and budgets with normal receipt-bound
  memory retrieval.

All arms use two proposal calls, two implementation opportunities, and two frozen
candidate evaluations. Codex token use is parsed from the JSONL completion events
and checked against the per-trial ceiling. The adapter creates one hash-manifested
local Git snapshot of the current source tree so candidate worktrees can execute
without changing the user's branch or requiring an implementation commit first.
Each runtime campaign re-locks its complete contract against that snapshot.

```powershell
$env:EVIDENCE_EVOLVE_CODEX_EXECUTABLE = `
  "C:\path\to\codex.exe" # optional when codex resolves normally

evolve benchmark run-graph-coloring `
  benchmarks/graph_coloring/three_arm_live_pilot_v0.locked.yaml `
  --run-dir runs/graph_coloring_three_arm_live_pilot_v0
```

This one-seed live pilot is an integration check, not the preregistered ten-seed
comparison. A future execution protocol must add externally held blind
confirmation before it can support a superiority claim. The public repository
cannot certify its own hidden confirmation set.
