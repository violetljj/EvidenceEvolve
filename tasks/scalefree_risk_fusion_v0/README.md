# ScaleFreeRiskFusion-v0

This is a self-contained **synthetic mechanics pilot** for discovering a
pure-visual relative-risk fusion formula. It does not contain BlindAssist data
and cannot establish real-world headroom, confirmation, deployment, product,
or safety authority.

## Mutable surface

EvoX may change only `candidate.py`, between `EVOLVE-BLOCK-START` and
`EVOLVE-BLOCK-END`. The candidate implements:

```python
compute_risk(features: dict[str, float]) -> float
```

The six inputs are normalized observations:

- `relative_nearness`: larger means visually nearer.
- `depth_approach_rate`: positive values usually mean approaching.
- `local_expansion`: rotation-compensated looming/expansion evidence.
- `path_intrusion`: intrusion into the forward image corridor.
- `depth_expansion_consistency`: agreement between approach and expansion.
- `observation_quality`: confidence in the current observation.

The output must be a finite deterministic scalar in `[0, 1]`.

## Frozen mechanics

The evaluator, synthetic generator, split seeds, metric definitions, negative
controls, and fitness aggregation are outside the evolve block. Candidates may
use basic Python arithmetic and the standard `math` module only. They may not
read files, inspect the evaluator, mutate benchmark state, import other
packages, use future observations, or emit abstentions/UNKNOWN.

The search fitness rewards approach Macro-F1, Spearman ranking, within-frame
pairwise ordering, low false-clear, low false-block, cross-parent stability,
negative-control degradation, and low code complexity. False-clear is retained
as its own metric and is not rescued by the aggregate score.

Every candidate evaluation is appended immediately to `evaluations.jsonl`, and
its exact source is copied to `candidates/evaluation_NNNN.py`. EvoX checkpoints
are emitted every iteration. The runner also retains every Codex event stream,
structured response, stderr log, token count, and final manifest.

