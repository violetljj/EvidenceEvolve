# EvidenceEvolve repository instructions

## Research authority

- GPT/Codex may propose, implement, and explain. Frozen code and deterministic evaluators decide gates.
- Never treat an LLM opinion, `combined_score`, prose summary, or leaderboard position as scientific evidence.
- Preserve the four scientific outcomes exactly: `POSITIVE_HEADROOM`, `VALID_NEGATIVE`, `NOT_EVALUABLE_DATA`, and `INVALID_MECHANICS_OR_ADAPTER`.
- `UNKNOWN` is not a negative. Missing eligible truth must remain `NOT_EVALUABLE_DATA`.
- False-clear and false-block are separate metrics. A hard constraint cannot be rescued by an aggregate score.
- Synthetic and mechanics evidence proves only mechanics. It does not establish real headroom, confirmation, deployment, product, or safety authority.

## Mutation boundaries

- Candidate work belongs in one candidate-specific Git worktree.
- Candidate agents may edit only `editable_scope.allow`; any change matching `editable_scope.deny` is protocol tampering.
- Evaluators, protocols, confirmation assets, evidence policies, closure rules, budgets, and Harness gate logic are immutable during a campaign.
- Closed families require externally verified reopen evidence. A candidate's own claim is never sufficient.
- Do not let multiple live writers operate in one worktree.

## Verification

- Use one focused implementation pass and one targeted verification pass.
- Run `python -m pytest` for governance or gate changes.
- Run the synthetic canary for changes to receipts, replay, budgets, protocol locking, closure enforcement, or outcome semantics.
- Do not claim Codex, ShinkaEvolve, ONNX/QNN, device, or confirmation integration passed unless the named external dependency and frozen assets actually ran.

## Resource-aware execution

- Treat effective, reproducible experimental results per wall-clock hour and per rental cost as execution-layer objectives. Rented compute should not remain idle without a protocol reason.
- Discover the resources actually available to the process before scheduling work. Respect CPU affinity and cgroup quota, visible GPUs, GPU memory, system memory, and scheduler limits; do not infer usable capacity from host specifications alone.
- Dynamically size worker counts, concurrent runs, and batches. Do not default to one CPU core or one serial run when independent seeds, blocks, configurations, proposal replays, compilation jobs, or evaluator calls can run concurrently.
- Pipeline independent I/O, preprocessing, compilation, CPU evaluation, and GPU work when doing so reduces idle time without changing frozen experimental semantics.
- Run a short, parallel mechanics admission or canary before an expensive campaign so shared adapter, evaluator, or infrastructure failures stop before consuming the formal budget.
- Avoid nested oversubscription. When running many independent evaluator processes, constrain per-process BLAS/OpenMP thread pools unless measured evidence supports a different allocation.
- For performance-sensitive runs, record the allocated CPU/GPU resources, worker and batch settings, wall time, throughput, CPU utilization, GPU utilization, and GPU-memory usage when those measurements are available and material.
- Resource optimization must not alter frozen sampling counts, seeds, arm ordering, budgets, failure handling, or eligibility rules. Never add retries or replacement samples to make one arm look better.
- When wall time itself is a comparison metric, isolate arms or give them fixed equivalent resource quotas. Fairness, determinism, and reproducibility take precedence over utilization.
- Use single-core or serial execution only when the algorithm or protocol is inherently serial, deterministic replay requires it, or concurrency would contaminate the comparison. Record the reason in the run receipt.
