# ENGINE_SELECTION_R1

`ENGINE_SELECTION_R1` selects a mature search backend after M4 stopped the
EvidenceEvolve search core. It does not reopen M4 and does not support a general
superiority claim.

The frozen design is:

- mechanics admission: four arms on one consumed task, 30k observed-token ceiling;
- core: Vanilla, AdaEvolve, ShinkaEvolve, and EvoX on three fresh tasks, two
  repeats, with 50k/100k/200k token checkpoints from one continuous run;
- reserve: the deterministic top two core arms on one pre-frozen fourth task,
  two repeats, only when the frozen ambiguity predicates fire;
- heldout seeds are created only after every candidate hash in the applicable
  stage is locked.

The protocol is `research/parity/engine_selection_r1.protocol.json`. Validate it
before execution:

```powershell
uv sync --extra dev --extra engine-selection-r1
.venv\Scripts\python.exe -m evidence_evolve.benchmarks.engine_selection_r1 validate-protocol
```

Run mechanics admission first. A failed receipt hard-blocks formal search:

```powershell
.venv\Scripts\python.exe -m evidence_evolve.benchmarks.engine_selection_r1_runner smoke --max-parallel 4
```

After a passing smoke receipt:

```powershell
.venv\Scripts\python.exe -m evidence_evolve.benchmarks.engine_selection_r1_runner search-core --max-parallel 4
.venv\Scripts\python.exe -m evidence_evolve.benchmarks.engine_selection_r1_runner finalize-core --max-parallel 4
```

Run the reserve only when `core_result.json` says `reserve_required: true`:

```powershell
.venv\Scripts\python.exe -m evidence_evolve.benchmarks.engine_selection_r1_runner search-reserve --max-parallel 4
.venv\Scripts\python.exe -m evidence_evolve.benchmarks.engine_selection_r1_runner finalize-reserve --max-parallel 4
```

Every model token class exposed by the adapters enters the same per-run ledger.
No new call is launched after the observed ceiling. Because a model call is
atomic, a call already in flight may cross the ceiling; all of its tokens remain
counted and the run becomes budget-invalid. No retry or replacement run is
allowed.
