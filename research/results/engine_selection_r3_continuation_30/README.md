# Engine Selection R3: 30-iteration continuation

Status: development-only continuation complete. This record does not open or
use held-out evidence and does not establish a held-out winner.

## Bound continuation

- Protocol: `research/parity/engine_selection_r3_continuation_30.protocol.json`
- Execution commit: `90f53ddb`
- Parent: `runs/engine_selection_r3_development_screen/attempt_05`
- Budget: 30 additional native iterations per arm and task; tokens were
  accounting-only and never a stopping rule.
- Clean result roots:
  - PDE: `runs/engine_selection_r3_continuation_30/attempt_02`
  - convex hull: `runs/engine_selection_r3_continuation_30/attempt_03`
  - communicability: `runs/engine_selection_r3_continuation_30/attempt_04`
- Excluded evidence: `attempt_02/convex_hull` was invalidated after a manual
  pipelining change briefly created concurrent writers. It is not used below.

## Final retained-candidate results

`Peak` is the best valid development observation during search. `Final` is the
retained candidate's final development re-evaluation and is the result used for
system comparison.

| Task | Arm | Parent | Peak | Final | Final change | Tokens | Wall seconds |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| PDE heat1d | Vanilla | 0.995617 | 0.998139 | 0.998139 | +0.253% | 807,353 | 1,315.8 |
| PDE heat1d | Ada | 2.294661 | 2.632074 | 2.505041 | +9.168% | 888,546 | 943.3 |
| PDE heat1d | Shinka | 1.006864 | 0.999756 | 0.999756 | -0.706% | 1,318,410 | 2,658.1 |
| PDE heat1d | EvoX | 2.312112 | 2.535615 | 2.444706 | +5.735% | 2,218,515 | 1,917.0 |
| convex hull | Vanilla | 1.012877 | 1.016615 | 1.009540 | -0.330% | 882,116 | 2,120.1 |
| convex hull | Ada | 1.008106 | 1.013405 | 1.007388 | -0.071% | 1,155,763 | 636.1 |
| convex hull | Shinka | 1.016314 | 22.457651 | 1.013034 | -0.323% | 1,648,871 | 1,182.3 |
| convex hull | EvoX | 23.489169 | 29.392937 | 26.591223 | +13.206% | 1,990,574 | 1,531.6 |
| communicability | Vanilla | 279.492982 | 296.105415 | 279.790577 | +0.106% | 790,824 | 1,398.3 |
| communicability | Ada | 256.083205 | 342.465788 | 316.037123 | +23.412% | 1,065,844 | 1,301.1 |
| communicability | Shinka | 0.990298 | 395.403772 | 1.018685 | +2.866% | 1,786,281 | 1,707.3 |
| communicability | EvoX | 178.428838 | 302.988878 | 299.946816 | +68.104% | 1,880,169 | 1,568.7 |

All 12 clean runs completed their 30 additional native iterations and recorded
`run_valid=true`.

## Development conclusions

For final retained-candidate quality, EvoX is the strongest and most robust
development result. It ranked second on PDE, first on convex hull, and second
on communicability. Its median final improvement was +13.206%, and all three
tasks improved. Ada won PDE and communicability, but did not improve convex
hull; its median final improvement was +9.168%.

The extra budget materially changed the picture. Ada overtook EvoX on PDE final
quality, EvoX retained a +13.2% convex-hull improvement, and both Ada and EvoX
made large communicability gains. Vanilla was effectively flat.

## Shinka selection failure

Shinka generated valid development candidates with 22.457651x convex-hull and
395.403772x communicability speedups, but its final selected candidate SHA was
identical to the parent candidate on both tasks. The native database recorded
`combined_score=0` for the proposals, so the large raw-speedup discoveries were
not promoted to final best. This separates candidate-generation ability from
end-to-end system delivery: the current Shinka integration can discover strong
algorithms but can discard them.

On PDE, Shinka also produced a pathological generation-35 candidate that used
the full 30-minute native job timeout. The timeout remained in the native
SQLite history and was not replaced or retried.

## Claim ceiling

These are development results on three already-bound R3 tasks. They support a
development recommendation of EvoX as `BEST_QUALITY_ENGINE` and Ada as a strong
second choice. They do not support a held-out superiority claim. A future fresh
protocol should repair Shinka's objective/selection mapping before comparing
its end-to-end quality again.
