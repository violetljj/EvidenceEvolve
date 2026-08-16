# BlindAssist EvoX real-ranking D0

This directory archives the public, reproducible result summary from a 30-loop
EvoX search over `compute_risk()` only. The candidate was selected after search
on a real parent-disjoint Development validation split.

The selected candidate improved validation fitness from
`0.2008952981096056` to `0.6393302713126698` while retaining full candidate
coverage. Loop 16 returned no evaluable program and remains a failed proposal;
it was not replaced.

There is no independent sealed test. The scientific outcome is therefore
`NOT_EVALUABLE_DATA`, and this archive makes no generalization, deployment, or
safety claim.

## Contents

- `best_candidate.py`: selected public candidate implementation.
- `analysis_report.md`: concise outcome and metric comparison.
- `final_result.json`: full baseline and selected validation metrics.
- `candidate_lock.json`: selected candidate identity and audit metadata.
- `integrity_audit.json`: campaign boundary and integrity checks.
- `adapter_repair.json`: recorded post-budget wrapper repair.
- `search_manifest.json`: immutable campaign and resource metadata.
- `iteration_summary.json`: public iteration-level summary with local snapshot
  paths removed.
- `pareto_front.json`: public Pareto records with local snapshot paths removed.

Private evaluator code, truth/features data, raw model-call logs, and temporary
execution environments are intentionally excluded.
