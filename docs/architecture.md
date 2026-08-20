# Architecture

Autonomous Math AI is a conjecture-neutral orchestration layer. A target owns
its mathematical statement, prompts, claim graph, canonical inputs, and local
runtime. The installed distribution owns protocols, policy enforcement,
lifecycle, scheduling, audit leases, and durable storage semantics.

## Package boundaries

- `engine/` controls dynamic admission and scheduling pressure.
- `protocol/` exposes Output Protocol v2 contracts, error classes, and schema
  compatibility preflight.
- `lifecycle/` owns monotone phases, campaign/epoch state, audit leases, and
  derived cognitive views.
- `storage/` owns atomic persistence, portable artifact references, steering,
  and local asset ingest.
- `mechanical/` owns the controller-brokered one-shot worker boundary.
- `provider_backend.py` routes roles to transport adapters; provider transport
  never owns mathematical role semantics or canonical-state authority.
- `provider_config.py` normalizes capability, effort, tier, usage, cost, and
  credential-reference declarations before any model turn.
- `cli/` owns the `amr` command surface.
- `resources/` contains immutable wire schemas and the bundled policy pack.

`storage_layer` is a compatibility import wrapper, not a second storage
implementation.

## Trust path

```text
research job
    │ emits an untrusted candidate
    ▼
identity + representation + schema checks
    │
    ▼
content-addressed immutable evidence bundle
    │
    ▼
fresh audit through a controller-owned lease
    │
    ▼
deterministic verification + canonical gate
    │
    ▼
trusted claim-state transition
```

Producer transcripts are not the trust source. The auditor receives the exact
statement, immutable bundle, representation contract, and requested audit kind.

## Campaign, epoch, and job

A campaign is a long-horizon research effort. It contains append-only epochs;
each epoch contains top-level jobs and isolated mechanical subtasks. Epochs are
the units of sealing, recovery, policy pinning, and handoff.

The lifecycle is monotone:

```text
BOOTSTRAP → RUNNING → DRAINING_* → SEALED
                    ↘ FINALIZING → COMPLETED
```

Once dispatch leaves `RUNNING`, a continuation cannot reopen the same epoch.
New information becomes a constraint for a later epoch. A crash resume remains
within the same epoch; campaign continuation creates a new one.

## Failure taxonomy

Failures are handled in this order:

1. local schema, bootstrap, canonical, or policy violation;
2. transport, rate-limit, or transient protocol failure;
3. a server-completed turn with failed status;
4. missing or invalid structured model output;
5. role-level semantic validation failure;
6. controller or state-machine failure.

A failed job is never passed into a role parser. Original server errors,
streamed events, identifiers, retry classification, and telemetry are retained.
Role protocol failures use bounded role-local retries. Controller, canonical,
policy, and local-schema failures drain the epoch as internal failures.

## Scheduling

Default hard caps are independent: one Director, eight research jobs, and eight
audits. Mechanical scheduling has no static seat cap by default, but the broker
derives a resource cap and enforces token/cost budget, queue, batch, rate-limit,
timeout, and stop backpressure. Main roles and mechanical workers use separate
governors (500 million and 1.5 billion default tokens respectively).
Dynamic admission adjusts only new dispatch based on information gain, route
novelty, estimated cost, and audit backlog. It never cancels healthy work merely
because the target concurrency changed.

Incremental Director work is coalesced and debounced behind a version watermark.
The Director does not wait for all research and audit jobs to drain and cannot
block their normal dispatch.

Every Director snapshot includes a controller-owned representation compatibility
view (claims grouped by representation id, known complete contracts, missing
contract ids, and independently audited bridge pairs) plus latest route state.
Route updates are durable bookkeeping, not runnable queue work. If semantic
admission rejects every proposed task and no audit priority is applicable, the
controller supplies the rejection reasons to one bounded repair turn; retry
exhaustion pauses the epoch instead of falling through to idle queue failure.

## Durable storage

Events and route records are append-only. Candidate artifacts are copied into
content-addressed bundles before audit. Durable references use `project://`,
`campaign://`, or `epoch://` URIs, so evidence does not depend on a machine's
absolute path.

`CORE_CAPSULE` is a bounded rebuildable snapshot, `RESEARCH_MAP` is a derived
human-readable view, and `ROUTE_LEDGER` records failed approaches and explicit
retry conditions. None of these derived views can override the canonical claim
graph.
