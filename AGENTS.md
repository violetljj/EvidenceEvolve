# EvidenceEvolve repository instructions

## Research authority

- GPT/Codex may propose, implement, and explain. Frozen code and deterministic evaluators decide gates.
- Never treat an LLM opinion, `combined_score`, prose summary, or leaderboard position as scientific evidence.
- Preserve the four scientific outcomes exactly: `POSITIVE_HEADROOM`, `VALID_NEGATIVE`, `NOT_EVALUABLE_DATA`, and `INVALID_MECHANICS_OR_ADAPTER`.
- `UNKNOWN` is not a negative. Missing eligible truth must remain `NOT_EVALUABLE_DATA`.
- False-clear and false-block are separate metrics. A hard constraint cannot be rescued by an aggregate score.
- Synthetic and mechanics evidence proves only mechanics. It does not establish real headroom, confirmation, deployment, product, or safety authority.

## Mutation boundaries

- Candidate work belongs in one candidate-specific Git worktree.
- Candidate agents may edit only `editable_scope.allow`; any change matching `editable_scope.deny` is protocol tampering.
- Evaluators, protocols, confirmation assets, evidence policies, closure rules, budgets, and Harness gate logic are immutable during a campaign.
- Closed families require externally verified reopen evidence. A candidate's own claim is never sufficient.
- Do not let multiple live writers operate in one worktree.

## Verification

- Use one focused implementation pass and one targeted verification pass.
- Run `python -m pytest` for governance or gate changes.
- Run the synthetic canary for changes to receipts, replay, budgets, protocol locking, closure enforcement, or outcome semantics.
- Do not claim Codex, ShinkaEvolve, ONNX/QNN, device, or confirmation integration passed unless the named external dependency and frozen assets actually ran.

