# Engine Selection R3 visible development screen

The persistent-transport successor attempt completed all 12 visible development
runs successfully. This screen spent no fresh blind task and cannot name a final
winner.

## Execution

- Run root: `runs/engine_selection_r3_development_screen/attempt_05`
- Execution commit: `a0a9908e1b32625183b181ed82a1e7b02a50f7b2`
- Protocol SHA-256: `3b0e3338c189441f288f9b27517c46ceef980c2c6b31636f4629d467106afc76`
- Transport: one persistent SSH stdio RPC channel per arm
- Completed runs: 12 / 12
- Token stops: 0
- Heldout evaluations: 0

## Final retained candidates

| Arm | PDE raw speedup | Convex hull raw speedup | Communicability raw speedup | Median improvement | Minimum improvement | Tokens (account only) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Ada | 2.2947 | 1.0081 | 256.0832 | 1.2947 | 0.0081 | 1,202,004 |
| EvoX | 2.3121 | 23.4892 | 178.4288 | 22.4892 | 1.3121 | 4,092,333 |
| Shinka | 1.0069 | 1.0163 | 0.9903 | 0.0069 | 0.0000 | 1,791,983 |
| Vanilla | 0.9956 | 1.0129 | 279.4930 | 0.0129 | 0.0000 | 996,329 |

The frozen arithmetic-mean rule orders the development signal as Vanilla, Ada,
EvoX, Shinka because the communicability scale dominates the mean. The
cross-task robustness fields tell a different and decision-relevant story: EvoX
is the only arm with more than `2x` raw speedup on every task and has by far the
largest median and minimum improvement. Ada is second on those stability fields.

This discrepancy must remain explicit. R3 is a visible consumed-task screen, not
heldout evidence. Any fresh-blind finalist gate must be frozen separately before
opening new tasks; R3 results must not be relabeled as a final engine ranking.
