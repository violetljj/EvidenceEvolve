# EvidenceEvolve

EvidenceEvolve is an evidence-gated, reproducible algorithm discovery harness. It separates creative search from scientific authority:

```text
candidate proposal -> isolated implementation -> deterministic evaluation
                   -> evidence/closure/safety gates -> immutable receipt
```

GPT/Codex can propose, implement, and explain candidates. It cannot edit campaign rules or decide its own fitness. Scientific decisions are derived from frozen contracts, raw metrics, controls, and deterministic rules.

## R0 scope

Implemented in this repository:

- research-contract validation and content locking;
- claim-aware candidate genomes;
- A/B/C/D evidence permissions;
- closure/reopen enforcement;
- hard-constraint and four-outcome gate semantics;
- idempotent budget accounting;
- immutable JSON receipts and deterministic replay;
- SQLite archive and lineage index;
- Git worktree scope auditing;
- Codex CLI command construction with role-specific sandboxes;
- an optional ShinkaEvolve integration seam;
- a synthetic trap canary for protocol tampering, missing evidence, safety regression, and a valid positive.

Not claimed by R0: an executed ONNX/QNN campaign, live Shinka evolution, blind confirmation, device deployment, algorithm novelty, or safety evidence.

## Setup

Python 3.11 is recommended because it matches ShinkaEvolve's documented development setup. The governance core also supports Python 3.12-3.14.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\evolve.exe --help
```

## First run

The repository ships an unlocked draft contract. Lock it against a committed baseline before using it as authority:

```powershell
evolve lock-contract research/contracts/synthetic_canary_r0.draft.yaml `
  --output research/contracts/synthetic_canary_r0.locked.yaml

evolve validate-contract research/contracts/synthetic_canary_r0.locked.yaml
evolve run-canary research/contracts/synthetic_canary_r0.locked.yaml
evolve inspect runs/synthetic_canary_r0
evolve replay runs/synthetic_canary_r0
```

`lock-contract` resolves `HEAD`, hashes every frozen asset, and seals the canonical contract payload. `validate-contract` then rejects drift, mutable evaluators, visible confirmation data, invalid evidence permissions, missing hard gates, and editable-scope conflicts.

## Codex and ShinkaEvolve

The Codex backend emits `codex exec --json --output-schema ...` commands and grants `workspace-write` only to the implementer role. Read-only roles use the default read-only sandbox. Authentication is deliberately outside contract files and receipts.

ShinkaEvolve remains an optional search kernel. Its score may schedule proposals, but EvidenceEvolve verdicts never read `combined_score`; they read the frozen multi-metric gate input.

