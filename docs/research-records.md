# Immutable research records and evaluation telemetry

Every new AMR epoch freezes the external state used for routing and appends a
structured terminal record. This layer is observational: it cannot promote a
claim, satisfy an authority audit, or weaken any candidate, representation, or
canonical transition gate.

## Run purpose and launch manifest

Use `--purpose` to separate ordinary research from development and controlled
experiments:

```powershell
amr run --project <project> --purpose NATURAL_RESEARCH
amr run --project <project> --purpose EVALUATION
```

The allowed values are `DEVELOPMENT`, `NATURAL_RESEARCH`, and `EVALUATION`.
Real runs default to `NATURAL_RESEARCH`; mock and dry runs default to
`DEVELOPMENT`. The purpose is pinned once under the campaign and cannot change
between epochs.

New `RUN_MANIFEST.json` files use schema v14. In addition to model routes,
budgets, configuration, output schemas, AMR source provenance, and canonical
state, they bind a content-addressed research context manifest. The launch
manifest is never rewritten. Actual end time and terminal hashes are committed
in an immutable final record after the epoch seals, because an unknown end time
cannot be added later without violating launch-manifest immutability.

The target-project Git identity records HEAD plus a SHA-256 fingerprint of the
exact project-local porcelain dirty state. Runtime provenance includes Python,
platform, AMR/Codex versions when observable, and selected CAS package versions.

## Frozen context and final snapshots

Each run writes:

```text
autonomous/runs/<epoch>/research_record/
├── CONTEXT_MANIFEST.json
├── context/
│   ├── config.json
│   ├── claim_graph.json
│   ├── trusted_state.json
│   ├── canonical_state.json
│   ├── frontier_before.json
│   ├── asset_registry.json
│   ├── representation_graph.json
│   ├── method_ledger.json
│   ├── audit_index.json
│   └── campaign_theme.json
├── results/<content-hash>.json
├── RESULT_INDEX.jsonl
└── final/
    ├── snapshots/frontier_after.<hash>.json
    ├── snapshots/frontier_delta.<hash>.json
    ├── metrics/<event-watermark>.<hash>.json
    ├── records/<event-watermark>.<hash>.json
    └── INDEX.jsonl
```

Missing optional inputs are recorded as `null`, not guessed. Every snapshot is
verified against its hash during replay. A resumed attempt appends a new
watermarked final record; it does not overwrite an earlier terminal snapshot.
The campaign also has an append-only `RESEARCH_RECORDS.jsonl` linking all epoch
records.

`frontier_delta` schema v2 directly names new audited/proved/falsified results,
new kill gates, closed obligations, external ingestions, reusable assets,
superseded objects, authority drift, and unresolved integration items. No
Markdown or free-form log parsing is required.

## Decision and reuse telemetry

For every schema-valid task candidate in a Director plan, the controller appends
`DIRECTOR_TASK_DECISION`, including the full normalized candidate, stable
fingerprint, admission stage, final admitted scope/dependencies/representation,
and a deterministic reason code. Codes include:

- `ALREADY_AUDITED`, `CLAIM_CLOSED`, `DUPLICATE_TASK`, and `KILL_GATED`;
- `THEME_EXCLUDED`, `WAIT_DEPENDENCY`, and `REPRESENTATION_BLOCKED`;
- `EXISTING_ASSET`, `AUDIT_CACHE_HIT`, and `BUDGET_REJECTED`;
- `ADMITTED` and `INVALID_TASK` for the non-suppression and residual cases.

Dispatch-time dependency drift, unavailable input closure, and budget rejection
are separate decision stages, so a task admitted from an older snapshot is not
misreported as having started.

Output Protocol v4 adds one `asset_usage` row for every id in the current task
packet's authoritative `research_context.loaded_asset_ids`. The worker
must say `USED` or `REJECTED`, give the exact reason, and state whether the asset
is cited in the result. Controller events distinguish `RETRIEVED`, `LOADED`,
`USED`, and `CITED_IN_RESULT`. Missing or mismatched rows are recorded as invalid
telemetry; AMR never infers use from context injection alone.

AuditKey decisions record hit/miss, the reused receipt, saved execution, and
available savings estimates. When a new object explicitly supersedes an audited
object, deterministic component comparison classifies statement,
representation, dependency, proof, certificate, source, or audit-policy change.
If no prior bound receipt exists, the reason is `NO_PREVIOUS_RECEIPT`. Unknown
historical token or wall-time savings remain `null` instead of becoming zero.

## Unified results and outcome taxonomy

Autonomous worker outputs and structured external Frontier results are normalized
to `unified_result.schema.json`. Mathematical status is limited to `PROVED`,
`FALSIFIED`, `COMPUTATION_ONLY`, or `OPEN`. Maturity, evidence, audit, authority,
routing effect, and representation remain separate fields.

The research outcome taxonomy preserves useful non-closure work:

- `PROVED_RESULT`, `USEFUL_NEGATIVE_RESULT`, `NEW_KILL_GATE`,
  `NEW_REUSABLE_ASSET`, and `COMPUTATION_ONLY`;
- `REPRESENTATION_BLOCKED`, `DEPENDENCY_BLOCKED`, and
  `BOUNDED_SEARCH_EXHAUSTED`;
- `NO_PROGRESS` and `INVALID_RESULT`.

A worker-reported proof may therefore have `math_status=PROVED` while its audit
and authority are still `PENDING`. This records what was produced without
confusing model output with a trusted theorem.

Every completed model job also emits `RESEARCH_COST_RECORDED` with token
breakdown, cost telemetry, elapsed time, model/provider/effort, and cost class
(`RESEARCH`, `AUDIT`, or `ROUTING`). Mechanical calls retain their existing
controller-owned attempt records.

## Deterministic metrics and replay

The terminal metrics summary is aggregated from structured events and job
records, never written by an LLM. It includes Director suppression counts, asset
retrieval/use, audit reuse and known/unknown savings, outcome counts, model and
mechanical calls, retries, token categories, known cost, summed job time,
elapsed epoch wall time, and Frontier delta counts.

Inspect or verify a run with:

```powershell
amr record inspect --project <project> --run-id <epoch>
amr record replay-context --project <project> --run-id <epoch>
amr record metrics --project <project> --run-id <epoch>
```

Replay reconstructs the external state visible to Director and workers; it does
not attempt to reproduce private model reasoning. Schema-v3 through schema-v13
run manifests remain byte-for-byte unchanged. The reader exposes a partial
legacy normalized view and explicitly lists context that older runs did not
freeze, preserving those runs as baseline and replay-benchmark evidence.

## Authority boundary

Research records are provenance and routing telemetry only. An externally
audited result may suppress duplicate routing, but canonical status still
requires the existing controller audit, semantic binding, integration, and
promotion pipeline. `CLAIMS.md`, `PROGRESS.md`, ClaimGraph, trusted state, and
formal authority are not writable through this layer.
