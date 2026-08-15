# EvidenceEvolve as an AI Research Operating System

EvidenceEvolve exists to do useful algorithm research, not to optimize the
novelty of its own memory architecture.

> Build a system that can actually do research first. Formalize why it works later.

The target loop is:

```text
problem model -> research state -> hypothesis -> research action
-> implementation/experiment -> frozen evidence -> diagnosis
-> research memory -> strategy adaptation -> next action
```

The unit of progress is not the number of generated candidates. It is a
reproducible reduction in uncertainty, a valid algorithm improvement, a useful
negative result, or a justified change of research direction per unit cost.

## Practical memory contract

Research Memory V2 exposes six useful views over immutable receipts:

1. `RESULT`: what was run, the observed metrics, and the frozen outcome.
2. `FAILURE`: the observed failure signature, evidence against the mechanism,
   and unresolved alternative explanations.
3. `MECHANISM`: the preregistered mechanism, expected signatures, observed
   signatures, support status, and applicability.
4. `LINEAGE`: the algorithm parent, mutation, commits, and patch bindings.
5. `FRONTIER`: unresolved questions and the cheapest available falsifier or
   ablation handle.
6. `PROCEDURE`: a reproducible command and the hashes that bound its validation
   range.

These are rebuildable scheduling projections. They never replace receipts,
metrics, patches, contracts, or evaluator output. Every card is source-bound,
versioned, and `SCHEDULING_ONLY`.

Role-scoped retrieval is a hard boundary. Confirmation-stage receipts are not
projected into agent memory, and the Gate Engine cannot retrieve derived cards.
SQLite structured filters and FTS5 provide the first useful retrieval layer;
graph, embedding, and cross-campaign transfer remain later additions that must
earn their complexity.

## Research Director contract

The Research Director selects the next research action, not a scientific
verdict. Its action vocabulary includes:

```text
MUTATE  REPLICATE  ABLATE  FALSIFY  COUNTEREXAMPLE  TRANSFER
SIMPLIFY  UNDERSTAND  SEARCH_LITERATURE  REPRODUCE
ACQUIRE_EVIDENCE  CLOSE  REOPEN  BREAKTHROUGH
```

The current executable slice can change proposal allocation toward controls,
simplification, failure-directed tests, transfer, representation changes, or
restarts. Its decision is persisted as a scheduling-only trace and supplied to
the Hypothesis Explorer. `SEARCH_LITERATURE` has an independent, source-bound
executor: it searches paper metadata, inspects pinned repository source, writes
an immutable action receipt, and can cause a same-generation Director
redecision. Other unsupported actions are reported as blocked rather than
silently represented as ordinary code mutations.

In particular, when evidence-bound frontier questions exist, the Director
allocates cheap discriminating actions before broad mutation. When one failure
family saturates or the frozen stagnation threshold is reached, it reallocates
search toward cross-family, representation, and restart mutations.

## Current capability boundary

Implemented in the first vertical slice:

- automatic receipt-to-memory compilation after mechanism assessment;
- Result, Failure, Mechanism, Lineage, Frontier, and Procedure cards;
- source hashes, claim ceilings, role firewall, FTS5/structured retrieval, and
  append-only retrieval audit events;
- a `memory-query` CLI for inspecting the same role-scoped packets used by the
  runner;
- a Research Director whose evidence-grounded decision changes mutation
  allocation in the autonomous loop;
- durable Research Action jobs, states, budgets, receipts, and idempotent resume;
- OpenAlex literature search and GitHub repository inspection with raw hashed
  snapshots and repository commit/tree/blob bindings;
- external Mechanism, Procedure, and Transfer cards that remain
  `INSPIRATION_ONLY` and `SCHEDULING_ONLY`;
- optional same-generation search, memory refresh, and Director redecision;
- frozen-contract binding for the memory compiler and R1 Director.

Not yet implemented, and therefore not claimed:

- independent `REPLICATE`, `ACQUIRE_EVIDENCE`, or standalone diagnostic
  experiment executors (their job states and budget vocabulary exist, but this
  is not execution capability);
- semantic closure or robust mechanism/failure clustering;
- cross-campaign transfer validation;
- learned research-policy promotion from held-out discovery outcomes;
- evidence that this system has produced a novel algorithmic breakthrough.

Those are the next product capabilities. Benchmarks and a paper story should
measure the working system after these loops exist; they are not the reason to
build the loops.
