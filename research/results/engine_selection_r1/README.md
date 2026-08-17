# ENGINE_SELECTION_R1 closeout

`ENGINE_SELECTION_R1` is closed as `INVALID_MECHANICS_OR_ADAPTER`. It produced
no engine ranking.

The v2 consumed-task smoke proved that all four adapters could execute with a
30k call-launch gate and a separate 100k atomic-call hard ceiling. The formal
protocol incorrectly used 200k as both the call-launch and hard ceilings. In the
first fresh block, every arm launched one last indivisible call below 200k and
finished above it:

| Arm | Observed tokens | Formal validity |
| --- | ---: | --- |
| Vanilla | 216,985 | invalid; interrupted before trajectory closeout |
| AdaEvolve | 223,811 | invalid |
| ShinkaEvolve | 202,756 | invalid |
| EvoX | 218,903 | invalid |

The orchestration was stopped after this shared failure became complete across
the first block. No heldout seeds existed, no candidate portfolio was locked,
and no pairwise score is permitted. Partial second-block work is preserved but
has no scientific authority.

`job_shop_scheduling` is consumed because model-driven search ran on it. The
other three pre-frozen task families received no search trajectory, heldout
seed, or evaluation. They remain eligible under a separately frozen successor
protocol. That successor records tokens as cost evidence only: token usage must
not stop iterations or invalidate an otherwise valid run.
