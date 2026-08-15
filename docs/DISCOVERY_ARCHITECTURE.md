# EvidenceEvolve discovery architecture

## Objective

EvidenceEvolve optimizes expected discovery value per unit of time, compute, and
agent budget. It does not optimize the amount of governance performed.

```text
maximize useful novelty, information gain, and robust improvement per cost
subject to evaluator integrity, blind evidence isolation, traceability, and
evidence-bounded claims
```

The operating principle is **maximum creativity internally, maximum rigor at the
claim boundary**. Early development may be fast, approximate, and mechanism-unknown.
Final claims may not be.

## Two-layer system

### Discovery Engine

The Discovery Engine owns proposal, code inheritance, mutation allocation,
stepping stones, failure-directed search, scientific memory, stagnation detection,
and future island or crossover scheduling. Its scores and mechanism assessments are
only scheduling inputs.

### Evidence Kernel

The Evidence Kernel owns the frozen evaluator, evidence eligibility, immutable
receipts, hard constraints, the four scientific outcomes, blind confirmation, and
claim ceilings. A discovery policy cannot modify the active campaign's Evidence
Kernel.

The four scientific outcomes remain exactly:

- `POSITIVE_HEADROOM`
- `VALID_NEGATIVE`
- `NOT_EVALUABLE_DATA`
- `INVALID_MECHANICS_OR_ADAPTER`

`UNKNOWN` or missing eligible truth is never converted into a negative.

## Three independent planes

Every evaluated candidate has three different meanings:

1. Evidence plane: the frozen `GateVerdict` and scientific outcome.
2. Search plane: whether the artifact is a `CODE_PARENT`,
   `FAILURE_DIRECTED_SEED`, `IDEA_INSPIRATION`, or `QUARANTINE` item.
3. Claim plane: the conservative maximum progression allowed by the stage, such as
   `DEVELOPMENT_ONLY` or `CONFIRMATION_ELIGIBLE`.

A valid negative can therefore remain useful as a failure-directed genetic seed
without becoming a positive scientific claim. A mechanics positive can be a code
parent while remaining development-only.

## Discovery funnel

The existing research stages implement a progressively stricter funnel:

| Discovery level | EvidenceEvolve stage | Minimum purpose |
| --- | --- | --- |
| Wild search | `M0_MECHANICS` | Runs, preserves frozen controls, produces metrics |
| Signal | repeated `M0` or task-defined DEV evaluation | Check that signal is not an immediate accident |
| Candidate | `H0_REAL_HEADROOM` | Stronger baseline and eligible real headroom |
| Scientific candidate | `T0_LEARNED_CANDIDATE` | Training, targeted ablation, robustness |
| Claim boundary | `C0_CONFIRMATION` | Blind confirmation and claim eligibility |
| Deployment boundary | `D0_DEPLOYMENT` | Separate device or deployment evidence |

Not every early candidate needs full ablation, mechanism proof, fresh seeds, or
confirmation. Those costs grow only when evidence justifies promotion.

## Implemented R1.1 semantics

The autonomous loop currently implements the following executable behavior:

- `authority_base_commit` remains the contract's frozen `campaign.base_commit`.
- every candidate declares one `genetic_parent_id` among its cited parents;
- its worktree is created from that parent's evaluated `candidate_commit`;
- committed candidates are pinned under immutable local `refs/evidence-evolve/...`
  refs so worktree cleanup does not erase genetic artifacts;
- receipts bind the genetic parent ID and commit, the cumulative baseline-relative
  patch hash, and the parent-relative patch hash;
- an empty parent-relative patch cannot receive a new candidate effect attribution;
- valid negatives may receive failure-directed parent rights;
- invalid mechanics or protocol artifacts are quarantined;
- one proposal or implementation failure is recorded in the generation result and
  does not terminate other candidates;
- the normal and breakthrough mutation mixes are converted into deterministic slot
  assignments rather than remaining decorative policy fields;
- policy fields without an executable parent, context, budget, or mutation effect
  have been removed from the formal schema instead of pretending to evolve;
- every generation writes a `policy_effect_trace.json` binding mode, parent pool,
  mutation assignments, context compiler, and moonshot slots;
- after the configured number of generations without a code-parent positive, the
  loop enters `BREAKTHROUGH` mode and uses structural, cross-family, or restart
  mutations;
- the configured moonshot fraction reserves proposal slots for breakthrough-style
  mutations even before global stagnation;
- the archive compiles immutable receipts into source-bound Result, Failure,
  Mechanism, Lineage, Frontier, and Procedure cards; retrieval is role-scoped,
  audited, and excludes confirmation evidence from agent memory;
- the Research Director turns eligible cards into a persisted next-action decision
  that changes mutation allocation while remaining `SCHEDULING_ONLY`.

These mechanics do not prove that the search is better than a baseline. They make
that comparison possible.

## Implemented R1.2 population semantics

R1.2 turns the last-generation parent list into a persistent, bounded population:

- population candidates, genotype hashes, island memberships, and migrations live
  in the resumable campaign SQLite database;
- each proposal slot is assigned to a policy-defined island before Codex runs;
- each island samples a bounded parent portfolio across `ELITE`, `NOVELTY`,
  `FAILURE`, `STEPPING_STONE`, and `MIGRANT` scheduling roles;
- novelty is keyed by a stable behavior-descriptor hash, with family, mutation type,
  and search abstraction as the fallback descriptor;
- a novel parent-rights artifact above the policy information-gain threshold is
  retained explicitly as a stepping stone;
- active island membership is capacity-bounded while deactivated history remains
  persistent and auditable;
- migration uses a snapshot of every source island and a deterministic ring, so a
  candidate cannot cascade through multiple islands in one migration event;
- the cumulative baseline-relative patch hash is claimed atomically before the
  frozen evaluator runs; an exact duplicate is recorded and the evaluator is not
  called again;
- proposal and evaluation execution use finite policy-bounded worker pools, with
  deterministic result ordering and candidate-local failure isolation;
- the policy effect trace records island assignments, parent pools and roles,
  migrations, mutation assignments, moonshots, and concurrency bounds.

Elite, novelty, information-gain, stepping-stone, and migration labels remain
search-plane scheduling metadata. They cannot alter the evidence-plane outcome or
claim-plane ceiling. Proposal-supplied diversity descriptors are not independent
novelty evidence; R1.3 must add calibrated and independently computed acquisition.

## Next executable slices

The order below is intentional. A feature is admitted only when it changes behavior
and has a focused test or campaign measurement.

### R1.3 Research intelligence

- sanitized code-context compiler containing relevant source and lineage diffs;
- **Implemented first slice:** OpenAlex mechanism cards and pinned GitHub
  repository Procedure/Transfer cards with source, applicability, raw snapshot,
  and action-receipt bindings;
- similarity retrieval over prior mechanisms, assumptions, and failure signatures;
- calibrated proposer predictions rather than self-awarded utility scores;
- independent acquisition using cost, calibration, redundancy, diversity, and
  portfolio coverage.

### R1.4 Research Action Grammar

The first vertical slice now records a scheduling-only Research Director decision
and maps the executable code-backed subset into actual mutation allocation:

```text
MUTATE  REPLICATE  ABLATE  FALSIFY  GENERATE_COUNTEREXAMPLE
TRANSFER  SIMPLIFY  UNDERSTAND  ACQUIRE_NEW_EVIDENCE  CLOSE_FAMILY
```

`MUTATE`, `ABLATE`, `FALSIFY`, `GENERATE_COUNTEREXAMPLE`, `TRANSFER`, `SIMPLIFY`,
and `BREAKTHROUGH` can currently alter candidate allocation and proposal context.
`SEARCH_LITERATURE` now runs as an independent source-bound action and can change
the Director decision before proposals are created. Evidence acquisition,
replication, and standalone diagnosis remain explicitly blocked until they have
their own attribution rules and executors; they are not silently disguised as
mutations.

### R1.5 Explorer, Scientist, and Red Queen

- Explorer populations optimize discovery and may retain mechanism-unknown
  performance signals.
- Scientist populations replicate, simplify, ablate, and explain promising
  artifacts.
- Red Queen populations generate candidate counterexamples and evaluator attacks.
  A frozen validity checker must approve any generated DEV adversarial case before
  it enters evaluation.

### R1.6 Semantic closure and calibrated research economy

- semantic closure should block renamed repetitions of the same failed mechanism;
- genuinely new supervision, representation, assumption, or observable signal may
  support a separately verified reopen request;
- proposers should predict outcome distributions and failure families;
- proper scoring rules should update proposer credibility;
- the research director should allocate token, compute, and experiment budgets by
  observed discovery value rather than prose confidence.

## Evaluation program

Architecture is not success evidence. A frozen task suite must compare at least:

```text
random mutation
vanilla Codex iteration
external evolutionary search kernel
EvidenceEvolve without scientific memory
full EvidenceEvolve
```

Primary measurements are valid blind improvement per cost, fresh-set robustness,
confirmation success, redundant experiment rate, proposal calibration, closure
violations, invalid candidate rate, and reproducibility. Aggregate scheduling scores
never rescue a failed hard constraint.

The repository now includes a three-arm graph-coloring protocol smoke that checks
paired seeds, equal budget ceilings, development-only selection, public-fresh
evaluation, and immutable trial receipts. Its built-in adapter uses the same solver
for all arms and must produce zero deltas. Because the fresh set is public and no
external confirmation authority is configured, this slice validates benchmark
mechanics only; it cannot satisfy the evaluation program or support a superiority
claim. See [`BENCHMARK_PROTOCOL.md`](BENCHMARK_PROTOCOL.md).

## Feature admission test

Before adding a governance or research mechanism, ask whether it measurably does at
least one of the following:

- increases useful discovery or information gain;
- reduces redundant search;
- helps escape local optima;
- prevents evaluator or evidence manipulation;
- protects blind confirmation;
- makes final claims more trustworthy.

If none applies, the mechanism does not belong on the main execution path.
