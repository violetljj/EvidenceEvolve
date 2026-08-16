# Archived evidence scope

This committed run bundle preserves the evidence needed to audit and analyze all 100 formal EvoX solution iterations:

- the 102-line evaluation ledger (baseline, 100 candidates, final reevaluation);
- all 102 candidate source snapshots;
- the iteration summary, manifest, candidate lock, post-lock holdout seed, final result, and analysis reports;
- all 127 `gpt-5.6-sol` call outputs, event streams, schemas, stderr logs, and receipts;
- all nine evolved search-strategy records;
- the upstream run log and final upstream best-program record.

The `upstream/checkpoints/` directory is deliberately omitted from Git. Its 100 directories occupy about 97 MB and mostly repeat the cumulative program database at every iteration. Iteration-level metrics and source snapshots remain fully represented by `evaluations.jsonl`, `iteration_summary.json`, `candidates/`, and the upstream log. The local run that produced this archive retained and audited all 100 checkpoint directories before publication.

The separately committed `runs/scalefree_risk_fusion_v0_sol_100/diagnostic_invalidation.json` records why the predecessor attempt was invalidated. It must not be combined with this repaired run's scientific or mechanics result.

This bundle is synthetic mechanics evidence only. Its scientific outcome remains `NOT_EVALUABLE_DATA`.
