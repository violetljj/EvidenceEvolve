# EvidenceEvolve repository instructions

## Maintainer workflow

- This repository has one independent maintainer. For ordinary repository work, use `master` by default rather than creating a feature branch.
- After completing and proportionately verifying an authorized change, commit it intentionally and push `master` to `origin` unless the user explicitly asks to keep the work local or uncommitted.
- Use a separate branch only when the user requests one, when an external contribution or review workflow requires it, or when high-risk/destructive work needs isolation. Candidate-specific campaign worktrees remain mandatory and do not change this repository-level default.
- Never bypass required verification, frozen campaign boundaries, or scientific governance merely to keep `master` moving.

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

## Benchmark reuse and evidence roles

- Do not create or consume a fresh task by default for each research iteration. Old tasks develop the system; fresh tasks adjudicate mature mechanisms.
- Maintain a long-lived benchmark bank. Each task record should bind at least `task_id`, family, difficulty, consumption level, historical runs, known failure modes, best known score, and whether it remains eligible for blind use.
- Treat benchmark roles as explicit evidence permissions:
  - `DEV`: high-frequency reuse is expected, including consumed tasks and frozen states such as Assignment, Eigenvalues, Rotate2D, M4, and Set Cover. Use them to tune controllers, parent selection, operators, budget policy, prompts, and search mechanisms.
  - `REGRESSION`: near-permanent reuse is expected to detect loss of previously demonstrated capability.
  - `VALIDATION`: reuse only sparingly across a small number of candidate versions. Once its outcomes influence repeated system changes, downgrade it to consumed validation or `DEV`; it is no longer held-out evidence.
  - `BLIND_FRESH`: preserve for final generalization tests after a mechanism has shown a clear signal on reused tasks. Ordinary mechanism development may use 100% consumed `DEV` and 0 fresh tasks.
- Use 70% reused tasks, 20% rotating tasks, and 10% truly fresh/blind tasks as a portfolio planning heuristic, not a mandatory per-round quota. Do not spend the fresh share merely to satisfy a ratio.
- Prefer paired control-versus-treatment comparisons with the same task, frozen initial state, model/provider version, budget, evaluator, and resource quota. Change only the mechanism under study unless the protocol explicitly declares another factor.
- Reusing a task changes its evidence role and claim ceiling. Results affected by repeated inspection or tuning cannot support held-out, fresh-generalization, confirmation, or superiority claims.
- Reuse does not reopen a sealed one-shot campaign or authorize mutation of its protocols, receipts, confirmation assets, or consumed run namespace. Reuse such material only through an explicitly labeled `DEV`/`REGRESSION` protocol or a bound development fixture that preserves the original evidence record.
- The scheduler should draw from the benchmark bank before requesting a new task. Open a fresh/blind cohort only when a predeclared development gate establishes that the expected information value justifies consuming it.
- `benchmark_bank/manifest.v1.yaml` is the canonical Core-12 registry. Validate its content lock and local asset hashes before selection; `CATALOG_ONLY` entries are planning references, not executable cases, and any task outside the bank requires an explicit manifest amendment or a predeclared fresh-gate exception.

## Verification

- On Windows, the repository-owned environment entry point is `pwsh -NoProfile -File scripts/project.ps1 <doctor|bootstrap|test|run|rebuild>`. Run `doctor` first and select the required `-Profile` (`dev`, `shinka`, `onnx`, or `algotune`). Do not use a global Python or ad hoc `pip install`; `.python-version`, `pyproject.toml`, and `uv.lock` are the local authority.
- The `rebuild` command may delete only a non-reparse-point `.venv` that resolves exactly inside this repository. It never cleans campaign worktrees, `runs/`, contracts, receipts, confirmation assets, or evidence.

- Use the repository environment at `/root/autodl-tmp/EvidenceEvolve/.venv`; invoke Python as `.venv/bin/python` and pytest as `.venv/bin/python -m pytest`. Do not probe the system Python first.
- Use one focused implementation pass and one targeted verification pass.
- Run `.venv/bin/python -m pytest` for governance or gate changes.
- Run the synthetic canary for changes to receipts, replay, budgets, protocol locking, closure enforcement, or outcome semantics.
- Do not claim Codex, ShinkaEvolve, ONNX/QNN, device, or confirmation integration passed unless the named external dependency and frozen assets actually ran.

## Resource-aware execution

- Treat effective, reproducible experimental results per wall-clock hour and per rental cost as execution-layer objectives. Rented compute should not remain idle without a protocol reason.
- For suitable EvidenceEvolve CPU-heavy work, prefer the configured AutoDL execution-only worker at `root@connect.westb.seetacloud.com:16288` over leaving independent work on the local machine. Use the repository's `evolve-remote` request/dispatch/verify workflow; never place credentials or scientific authority on the worker.
- Treat the current AutoDL allocation as at most 32 process-visible vCPUs and 60 GiB memory, but probe affinity and cgroup limits at dispatch time because the live allocation may change. Dynamically fill useful capacity for independent tests, benchmarks, compilation, simulation, and candidate evaluation without exceeding the job's declared worker ceiling.
- Pull back and verify receipts, logs, and declared artifacts after remote execution. When no further queued work justifies rental time, explicitly remind the user to stop the instance in the AutoDL console; do not assume SSH process exit stops billing.
- Discover the resources actually available to the process before scheduling work. Respect CPU affinity and cgroup quota, visible GPUs, GPU memory, system memory, and scheduler limits; do not infer usable capacity from host specifications alone.
- Dynamically size worker counts, concurrent runs, and batches. Do not default to one CPU core or one serial run when independent seeds, blocks, configurations, proposal replays, compilation jobs, or evaluator calls can run concurrently.
- Pipeline independent I/O, preprocessing, compilation, CPU evaluation, and GPU work when doing so reduces idle time without changing frozen experimental semantics.
- Run a short, parallel mechanics admission or canary before an expensive campaign so shared adapter, evaluator, or infrastructure failures stop before consuming the formal budget.
- Avoid nested oversubscription. When running many independent evaluator processes, constrain per-process BLAS/OpenMP thread pools unless measured evidence supports a different allocation.
- For performance-sensitive runs, record the allocated CPU/GPU resources, worker and batch settings, wall time, throughput, CPU utilization, GPU utilization, and GPU-memory usage when those measurements are available and material.
- Resource optimization must not alter frozen sampling counts, seeds, arm ordering, budgets, failure handling, or eligibility rules. Never add retries or replacement samples to make one arm look better.
- When wall time itself is a comparison metric, isolate arms or give them fixed equivalent resource quotas. Fairness, determinism, and reproducibility take precedence over utilization.
- Use single-core or serial execution only when the algorithm or protocol is inherently serial, deterministic replay requires it, or concurrency would contaminate the comparison. Record the reason in the run receipt.
