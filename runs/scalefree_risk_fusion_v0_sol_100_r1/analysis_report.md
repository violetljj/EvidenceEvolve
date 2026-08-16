# EvoX scale-free risk fusion: 100-iteration analysis

## Outcome

The 100 requested solution iterations completed and are fully recorded. Mechanics status is `PASS`; the scientific outcome is `NOT_EVALUABLE_DATA` because this repository does not contain eligible real BlindAssist feature tables and labels. All results below are synthetic mechanics evidence only.

## Record-integrity audit

| Check | Result |
|---|---:|
| Formal solution iterations | 100/100, contiguous 1–100 |
| Evaluation ledger | 102 records, contiguous 0–101 |
| Roles | 1 baseline + 100 candidates + 1 final reevaluation |
| Candidate source snapshots | 102 |
| Source hash mismatches | 0 |
| Invalid evaluations | 0 |
| Unique candidate hashes | 89 |
| Checkpoints | 100, checkpoint every iteration |
| Evolved search strategies | 9 |
| Sol call receipts | 127/127 passed |
| Model | `gpt-5.6-sol`, high reasoning |
| Full repository tests | 101 passed |

## Main result

The development baseline scored 0.929695. Candidate 83 was the best aggregate candidate at 0.930615, a gain of 0.000920. It added a small reliability-gated interaction term while remaining scale-free and bounded:

```python
0.30 * relative_nearness
+ 0.30 * positive_depth_approach
+ 0.22 * positive_local_expansion
+ 0.18 * path_intrusion
+ 0.04 * (consistency + quality - 1.0) * min(approach, expansion)
```

| Development metric | Baseline | Candidate 83 | Difference |
|---|---:|---:|---:|
| Combined score | 0.929695 | 0.930615 | +0.000920 |
| Macro-F1 | 0.893992 | 0.891950 | -0.002042 |
| False-clear | 0.056507 | 0.038527 | -0.017979 |
| False-block | 0.080673 | 0.095473 | +0.014800 |
| Spearman | 0.940216 | 0.944907 | +0.004691 |
| Pairwise accuracy | 0.887220 | 0.891667 | +0.004447 |
| AST nodes | 80 | 96 | +16 |

The aggregate winner therefore trades fewer false-clears for more false-blocks. Its higher combined score is not evidence of a safety improvement.

## Important tradeoff candidates

- Candidate 78 was the only candidate that improved both development false-clear and false-block versus baseline. It also had the best Spearman (0.947742) and pairwise accuracy (0.895358), but its complexity was 190 and combined score 0.917257.
- Candidate 55 had the highest Macro-F1 (0.906091), with false-clear 0.077055 and false-block 0.056007.
- Candidate 42 minimized false-clear (0.013699) but raised false-block to 0.122171.
- Candidate 36 minimized false-block (0.027858) but raised false-clear to 0.693493.
- Candidate 33 reproduced baseline behavior with only 67 AST nodes.

Across the 100 candidates, 24 strictly exceeded the development baseline aggregate score. The median was 0.927439, the mean was 0.920244, and the range was 0.794286–0.930615.

## Post-lock synthetic holdout

The holdout seed was generated only after candidate 83 was locked. A post-hoc baseline reference was then evaluated with the same frozen evaluator and seed without adding a record to the formal 102-line ledger.

| Holdout metric | Post-hoc baseline | Candidate 83 | Difference |
|---|---:|---:|---:|
| Combined score | 0.928235 | 0.929218 | +0.000983 |
| Macro-F1 | 0.890917 | 0.887686 | -0.003231 |
| False-clear | 0.058304 | 0.037986 | -0.020318 |
| False-block | 0.078228 | 0.090810 | +0.012582 |
| Spearman | 0.940097 | 0.944407 | +0.004311 |
| Pairwise accuracy | 0.884905 | 0.889388 | +0.004483 |
| AST nodes | 80 | 96 | +16 |

The same tradeoff persisted on the synthetic holdout. This consistency is mechanics evidence, not real-data confirmation.

## Execution notes

The run took 6,230.18 seconds (about 1 h 43 min 50 s) and retained 127 complete model-call receipts. Reported token usage was 2,698,516 input, 2,029,568 cached input, and 286,419 output tokens.

The first evolved search strategy raised a database `KeyError` before formal solution iteration 12. EvoX rolled it back with zero new programs preserved, then evaluated formal iteration 12 exactly once. The event is retained in the upstream log and did not create a ledger gap or consume an extra formal candidate.

An earlier attempt is separately marked `INVALID_MECHANICS_OR_ADAPTER` because runtime jitter had incorrectly influenced fitness. The repaired run excludes runtime from aggregate fitness while retaining it as a diagnostic.

## Next evidence required

To move beyond `NOT_EVALUABLE_DATA`, freeze a real BlindAssist table with the six scale-free inputs, eligible risk labels or pairwise ordering truth, parent/video grouping, and untouched confirmation split. Then replay the locked baseline, candidate 83, and the most relevant tradeoff candidate (especially candidate 78) under hard false-clear and false-block gates rather than relying on aggregate score alone.
