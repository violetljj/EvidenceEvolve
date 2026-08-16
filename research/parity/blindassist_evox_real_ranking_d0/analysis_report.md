# BlindAssist EvoX Real Ranking D0 — 30-loop result

## Outcome

`BEAT_BASELINE_ON_REAL_PARENT_DISJOINT_VALIDATION`

| VALIDATION metric | Baseline | Selected candidate | Delta |
|---|---:|---:|---:|
| Combined Development fitness | 0.200895298 | 0.639330271 | +0.438434973 |
| T1 parent-macro Spearman | -0.013689 | 0.026046 | +0.039735 |
| T2 parent-macro pairwise | 0.118265 | 0.610781 | +0.492516 |
| T3 parent-macro pairwise | 0.103120 | 0.718318 | +0.615198 |
| Candidate coverage | 1.000000 | 1.000000 | +0.000000 |
| AST nodes | 176 | 39 | -137 |

EvoX completed 30 formal solution loops. Loop 16 produced no evaluable program; it was
retained as a failed proposal and was not replaced. The remaining 29 candidate evaluations
yielded 18 unique evolved sources. VALIDATION was evaluated only after the search
ended, across the unique frozen candidate snapshots, and selected the candidate above.

Candidate execution used PUBLIC rows only under the `nobody` account. The private evaluator
ran separately as root. Every evaluated source passed the compute_risk-only byte/AST audit.

This beats baseline on real parent-disjoint Development ranking. There is no SEALED_TEST, so
the scientific outcome remains `NOT_EVALUABLE_DATA`; this is not a generalization or safety claim.
