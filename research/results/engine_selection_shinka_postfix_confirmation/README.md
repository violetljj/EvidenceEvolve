# Shinka post-fix selection confirmation

Status: `PASS`. This is a development-mechanics confirmation only. It does not
use fresh or held-out evidence, rerun another engine, or establish comparative
superiority.

## Frozen scope

- Protocol: `research/parity/engine_selection_shinka_postfix_confirmation.protocol.json`
- Execution commit: `9e7ebf5dba89d68fff03d8a2bbac09999d17560a`
- Clean result root: `runs/engine_selection_shinka_postfix_confirmation/attempt_04`
- Engine: Shinka only
- Tasks: PDE heat1d, convex hull, and communicability
- Budget: two clean runs per task, 12 native iterations per run, six runs total
- Historical comparison: read-only R3 pre-fix Shinka trajectories; the other
  engines were not rerun.

Attempts 01 through 03 are excluded. Attempt 01 used a stale remote worker;
attempt 02 exposed a cross-platform implementation-hash mismatch; attempt 03
was diagnostic evidence for a CRLF-sensitive selected-candidate audit. The
portable bindings and audit normalization were frozen before attempt 04.

## Result

All six runs passed all eight frozen mechanics gates (`48/48`). Across 78
recorded candidates, 50 were valid. Every valid candidate had
`combined_score == raw_speedup`; every invalid candidate had
`combined_score == 0`. There were no raw-speedup order inversions, no better
candidate generated but not promoted, and every returned candidate was the
run's highest valid candidate under Shinka's formal selection rule.

`Promotions` counts generation 0, so `new promotions` excludes the initial
program.

| Task | Repeat | Valid / total | Promotions | New promotions | Final raw speedup | Gates |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| PDE heat1d | 1 | 1 / 13 | 1 | 0 | 1.037981 | PASS |
| PDE heat1d | 2 | 1 / 13 | 1 | 0 | 1.049179 | PASS |
| Convex hull | 1 | 12 / 13 | 7 | 6 | 1.029082 | PASS |
| Convex hull | 2 | 13 / 13 | 2 | 1 | 1.019683 | PASS |
| Communicability | 1 | 11 / 13 | 5 | 4 | 317.006695 | PASS |
| Communicability | 2 | 12 / 13 | 4 | 3 | 314.582763 | PASS |

The complete per-generation records are retained in `audits/`. Each row binds
the raw speedup, mapped combined score, candidate ID, promoted candidate ID,
current historical-best ID, validity, and the better-but-not-promoted flag.
`result.json` is the compact six-run summary.

## Comparison with the pre-fix trajectory

The historical 30-iteration record showed the failure mode directly: Shinka
generated convex-hull and communicability peaks of `22.457651x` and
`395.403772x`, while their proposal `combined_score` values remained zero and
the final retained candidates fell back to `1.013034x` and `1.018685x`.

The post-fix runs no longer show that disconnect. Convex hull promoted seven
new historical bests across the two repeats and retained the best valid result
in each run. Communicability produced an especially clear end-to-end signal:
the two selection histories climbed from roughly `1x` to `317.006695x` and
`314.582763x`, respectively, and later lower or invalid candidates did not
displace those maxima. PDE produced no valid proposal in either short run, so
it confirms invalid-score handling and best retention but provides no positive
candidate-generation signal.

## Claim ceiling and next decision

The repair is end-to-end confirmed for the observed development mechanics.
The result supports considering a newly frozen 30-iteration fair rematch if an
updated cross-engine ranking is worth the compute cost. It does not itself
authorize that rematch, and historical versus post-fix speedup magnitudes are
not a paired performance comparison.
