# ENGINE_SELECTION_R2_EFFECT_FIRST

R2 asks one primary question: which of Vanilla, AdaEvolve, ShinkaEvolve, and
EvoX discovers the strongest final heldout algorithm when every system gets the
same model, task information, initial program, and wall-time envelope?

Tokens are account-only evidence. They never stop an iteration, invalidate a
run, or outrank final quality, stability, reproducibility, search speed, or wall
time. They appear only as the final tie-break when the effect evidence remains
equivalent under the frozen 0.01 improvement band.

The tournament contains 24 formal runs:

- round one: four engines, three never-executed tasks, one repeat (12 runs);
- final: the deterministic top two, the same tasks, two fresh repeats (12 runs).

Every run permits up to 30 engine-native iterations inside a 2700-second wall
envelope. Equal model-call counts are not required. The engines may use their
native lineage, archive, population, and meta-search behavior.

Run the four-arm consumed-task mechanics admission, then start round one:

```powershell
.venv\Scripts\python.exe -m evidence_evolve.benchmarks.engine_selection_r2_runner smoke --max-parallel 4
.venv\Scripts\python.exe -m evidence_evolve.benchmarks.engine_selection_r2_runner search-round-1 --max-parallel 4
```

After all final candidate hashes are locked, create fresh heldout seeds and
score round one:

```powershell
.venv\Scripts\python.exe -m evidence_evolve.benchmarks.engine_selection_r2_runner finalize-round-1 --max-parallel 4
.venv\Scripts\python.exe -m evidence_evolve.benchmarks.engine_selection_r2_runner search-final --max-parallel 4
.venv\Scripts\python.exe -m evidence_evolve.benchmarks.engine_selection_r2_runner finalize-final --max-parallel 4
```

The primary output is `BEST_QUALITY_ENGINE`; `MOST_ROBUST_ENGINE` and
`FASTEST_ENGINE` are separately reported.
