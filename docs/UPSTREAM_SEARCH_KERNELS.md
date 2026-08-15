# Upstream search kernels and parity gates

## Principle

EvidenceEvolve is a research operating system, not a reason to reimplement a
weaker version of a mature search engine. The default admission rule is:

> Integrate the pinned upstream implementation first. Do not replace it with an
> EvidenceEvolve-native component until an external, equal-budget, multi-seed
> non-inferiority test passes.

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

Four separate checks are required:

1. **Deterministic construction parity.** With a capturing runner, direct upstream
   CLI construction and `SHINKA_NATIVE` must receive identical effective config
   and task source. The imported receipt must preserve the runner-produced
   candidate, lineage, metric, cost, and token records. This check is covered by
   `tests/test_shinka_native.py`.
2. **Deterministic runner parity.** The actual pinned runner, driven by the same
   deterministic fake model/provider, must produce identical candidate/parent
   sequences and metric events through the direct CLI and `SHINKA_NATIVE` paths.
   This check is not yet implemented.
3. **Real non-inferiority.** On the official circle-packing task with paired
   seeds, identical model/config/evaluator/budget/hardware, the median normalized
   best score must not fall more than 1% below direct Shinka. Best-so-far AUC,
   first valid improvement, cost, invalid rate, rejection rate, throughput, and
   resume consistency must also be reported.
4. **Evidence enhancement.** `SHINKA_EVIDENCE` must be non-inferior to
   `SHINKA_NATIVE` on at least three tasks and five seeds per task before it can
   become the default.

Only construction parity is currently satisfied. No promotion gate has passed.
The engine integration is not evidence that circle packing, ALE-Bench, algorithm
discovery, or scientific superiority passed.
