# AlgoTune Set Cover horizon scaling v0

This bundle freezes the completed development-search trajectories at horizons 3, 6, 12, 24, and 50.

**Held-out seeds have not been generated and held-out evaluation has not run. These numbers are not a scientific cross-engine ranking.**

| Arm | h50 dev speedup | tokens | wall | valid rate | exploratory shape |
|---|---:|---:|---:|---:|---|
| shinka | 3.4927x | 1,032,255 | 1.82 h | 98.0% | EARLY_MATURE_BY_H24 |
| ada | 7.0518x | 1,240,486 | 1.90 h | 100.0% | SUSTAINED_IMPROVEMENT |
| evox | 13.4818x | 1,698,566 | 1.98 h | 96.0% | LATE_PUNCTUATED_BREAKTHROUGH_SIGNAL |
| evidence_evolve | 1.0272x | 8,259,522 | 2.73 h | 72.0% | PLATEAU_LOCAL_OPTIMUM |

`curve.csv` contains all 20 checkpoint rows. `trajectories/` preserves full trajectory metadata, and `candidates/` preserves every checkpoint source with hashes recorded in `result.json`.
