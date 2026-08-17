# ENGINE_SELECTION_R2_EFFECT_FIRST closeout

R2 is closed as `INVALID_MECHANICS_OR_ADAPTER`; it produced no engine ranking.

All four PDE arms timed out after 2880 seconds. Tokens did not stop any arm. The
first ten AutoDL evaluations completed in 2.66 to 4.21 seconds, after which four
transport requests never produced locally verified receipts and occupied the two
dispatch slots. The scheduler then incorrectly launched the next paired block.
The controller stopped the stage and verified that no task-owned local process or
remote `engine-r2` worker remained.

No heldout seed, heldout evaluation, candidate lock, or round-one result exists.
Ada's partial development score of 2.07 raw speedup is diagnostic only and is not
a winner result.

Before a successor run, the execution layer must prove bounded SSH/SCP transport,
owned process-tree termination, append-only per-evaluation observations, and
paired-block fail-fast under a sustained transport admission.
