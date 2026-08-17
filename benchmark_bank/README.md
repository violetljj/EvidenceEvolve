# EvidenceEvolve Benchmark Bank v1

This directory is the canonical long-lived task inventory for EvidenceEvolve.
The default is to reuse this bank. A new task or blind cohort requires the fresh
gate in `manifest.v1.yaml`; it is not a routine per-iteration dependency.

## Current authority

`manifest.v1.yaml` freezes the Core-12 catalog, evidence roles, difficulty tiers,
source references, selection templates, and claim ceilings. All 12 families have
a tracked regression smoke case and passed the repository verifier. Official
source cohorts and verifiers are cached under the ignored `assets.local/` tree;
`materialization_receipt.v1.yaml` binds each cache file to its source revision,
byte length, and SHA-256 without committing hundreds of megabytes to Git.

The status `CORE12_MATERIALIZED_SMOKE_ADMITTED` means format, feasibility, and
objective recomputation work for one frozen tiny case per family. It does **not**
mean every family has a production-scale runner, that every public instance has
been executed, or that any scientific comparison passed. No v1 asset is blind:
historical PACE public/private data and public MiniZinc or DIMACS data remain
public benchmark evidence.

Validate the catalog and its local bindings with:

```powershell
.venv\Scripts\python.exe -m evidence_evolve.benchmark_bank --repo . validate benchmark_bank/manifest.v1.yaml
```

Deep-check every cached archive (SHA-256, size, safe member names, and ZIP CRC or
full TAR payload readability) with:

```powershell
.venv\Scripts\python.exe scripts/verify_benchmark_bank_assets.py --repo . --receipt benchmark_bank/materialization_receipt.v1.yaml --deep
```

Create a deterministic family-level development plan with:

```powershell
.venv\Scripts\python.exe -m evidence_evolve.benchmark_bank --repo . select benchmark_bank/manifest.v1.yaml --template routine_dev_3x3 --seed 170817
```

Selections carry `NO_SCIENTIFIC_CLAIM` authority. Smoke admission is only the
minimum mechanics gate before a selected family receives a frozen runner,
budget, resource policy, and experiment-specific instance inventory.

## Core-12

| Tier | Families | Maximum authority in this public bank |
| --- | --- | --- |
| L0 | Assignment, Knapsack | Screening only |
| L1 | Set Cover, Graph Coloring, Steiner Tree, CVRP, Flexible Job Shop | Screening only |
| L2 | Cluster Editing, Directed Feedback Vertex Set | Development comparison only |
| L3 | Twinwidth | Research-value evaluation eligible |
| L4 | Dominating Set | Research-value evaluation eligible |
| L5 | Maximum Agreement Forest | Research-value evaluation eligible |

“Eligible” is a ceiling, not a result. L3–L5 results still require valid paired
execution, frozen evaluators and budgets, complete receipts, and an appropriate
protocol. Public bank results never establish blind generalization or superiority.

## Selection policy

- `routine_dev_3x3`: three reusable/regression families and three instances per
  family. This is the default mechanism-development shape.
- `signal_validation_8x5`: eight public benchmark families and five instances per
  family, opened only after a development signal.
- `milestone_core12`: five instances from every Core-12 family.
- The 70/20/10 reuse/rotation/fresh split is a portfolio heuristic, not a quota.
  Daily development can and usually should spend zero fresh tasks.

Use the same task, initial state, model/provider version, evaluator, budget and
resource quota for paired control/treatment comparisons. Once validation feedback
changes the system, downgrade that evidence to consumed validation or DEV.

## Local materialization

Large upstream assets are intentionally not redistributed through this Git
repository. To audit the current machine, run the validation command above. A
missing ignored cache produces `RECEIPT_ASSET_NOT_LOCAL`; a present file with the
wrong length or SHA-256 fails closed.

Adding or refreshing an upstream cohort is a separate evidence-producing change:

1. Review source use terms. The receipt's
   `LOCAL_CACHE_NO_REDISTRIBUTION_CLAIM` deliberately makes no redistribution
   claim.
2. Pin an immutable upstream revision or archive URL; download into the declared
   repository-owned asset location.
3. Compute SHA-256 for the archive, verifier, scorer, and every instance inventory
   manifest. Never infer or copy an unverified hash.
4. Record stable instance IDs, roles, provenance, known optimum or bound status,
   and verifier command in an experiment-specific inventory.
5. Bind the asset through the receipt, update the bank content lock, and rerun
   validation and focused tests.

Formal one-shot campaign namespaces remain closed. Reusing their task structure
requires a separately labeled DEV/REGRESSION fixture and never mutates old receipts.

## Verified official references

- [MiniZinc Challenge problem catalog](https://www.minizinc.org/challenge/globals/)
- [PACE 2018 Steiner Tree](https://pacechallenge.org/2018/steiner-tree/)
- [PACE 2021 Cluster Editing](https://pacechallenge.org/2021/)
- [PACE 2022 Directed Feedback Vertex Set](https://pacechallenge.org/2022/tracks/)
- [PACE 2023 Twinwidth](https://pacechallenge.org/2023/)
- [PACE 2025 Dominating Set](https://pacechallenge.org/2025/ds/)
- [PACE 2026 Maximum Agreement Forest and STRIDE](https://pacechallenge.org/2026/)
